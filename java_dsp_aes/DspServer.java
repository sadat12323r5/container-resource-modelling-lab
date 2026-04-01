import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import javax.crypto.Cipher;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.io.*;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.Base64;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * java-dsp — End-to-end encrypted signal processing server.
 *
 * Architecture: plain Java HttpServer backed by a FIXED thread pool of JAVA_WORKERS
 * threads. This is a true M/G/c server: exactly c requests execute simultaneously,
 * additional arrivals wait in a bounded queue (JAVA_QUEUE_CAPACITY).
 *
 * Pipeline: AES-256-CBC decrypt -> FIR low-pass filter -> AES-256-CBC encrypt
 * Protocol: POST /process {iv, payload} -> {ok, iv, payload, samples, service_ms}
 */
public class DspServer {

    // -----------------------------------------------------------------------
    // Config (from environment variables)
    // -----------------------------------------------------------------------
    private static final int    PORT           = intEnv("PORT",                3000);
    private static final int    WORKERS        = intEnv("JAVA_WORKERS",        4);
    private static final int    QUEUE_CAP      = intEnv("JAVA_QUEUE_CAPACITY", 1024);
    private static final int    SIG_LEN        = intEnv("DSP_SIGNAL_LENGTH",   1024);
    private static final int    FILTER_TAPS    = intEnv("DSP_FILTER_TAPS",     64);
    private static final double CUTOFF         = dblEnv("DSP_CUTOFF_FREQ",     0.25);
    private static final String CSV_FILE       = System.getenv().getOrDefault("DSP_TRACE_CSV", "");
    private static final byte[] AES_KEY        = parseHexKey(
            System.getenv().getOrDefault("DSP_AES_KEY",
            "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"));

    // Pre-computed FIR kernel (shared, read-only after init)
    private static final double[] KERNEL = buildHammingFIR(FILTER_TAPS, CUTOFF);

    private static final SecureRandom RNG     = new SecureRandom();
    private static final AtomicBoolean CSV_HDR = new AtomicBoolean(false);

    // -----------------------------------------------------------------------
    // Main
    // -----------------------------------------------------------------------
    public static void main(String[] args) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(PORT), QUEUE_CAP);

        server.createContext("/process", new ProcessHandler());
        server.createContext("/health",  exchange -> sendJson(exchange, 200, "{\"ok\":true}"));
        server.createContext("/",        exchange -> sendJson(exchange, 200,
            String.format("{\"service\":\"java-dsp\",\"runtime\":\"Java fixed thread pool (c=%d)\"," +
                "\"pipeline\":\"AES-256-CBC decrypt -> FIR low-pass -> AES-256-CBC encrypt\"," +
                "\"workers\":%d,\"signal_length\":%d,\"filter_taps\":%d,\"cutoff\":%.2f}",
                WORKERS, WORKERS, SIG_LEN, FILTER_TAPS, CUTOFF)));

        // Fixed thread pool: exactly WORKERS threads, no dynamic scaling.
        // This is M/G/c: c = WORKERS. Arrivals queue at the HttpServer accept queue.
        ThreadPoolExecutor pool = new ThreadPoolExecutor(
            WORKERS, WORKERS, 0L, TimeUnit.MILLISECONDS,
            new LinkedBlockingQueue<>(QUEUE_CAP),
            new ThreadPoolExecutor.AbortPolicy()
        );
        server.setExecutor(pool);
        server.start();
        System.out.printf("java-dsp :%d  workers=%d  taps=%d  cutoff=%.2f  siglen=%d%n",
            PORT, WORKERS, FILTER_TAPS, CUTOFF, SIG_LEN);
    }

    // -----------------------------------------------------------------------
    // POST /process handler
    // -----------------------------------------------------------------------
    static class ProcessHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            long arrivalNs = Instant.now().toEpochMilli() * 1_000_000L;
            long reqStart  = System.nanoTime();

            if (!"POST".equals(exchange.getRequestMethod())) {
                sendJson(exchange, 405, "{\"error\":\"method_not_allowed\"}");
                return;
            }

            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            String ivB64, payloadB64;
            try {
                ivB64      = extractJsonString(body, "iv");
                payloadB64 = extractJsonString(body, "payload");
            } catch (Exception e) {
                sendJson(exchange, 400, "{\"error\":\"missing iv or payload\"}");
                return;
            }

            // ---- SERVICE WORK ----
            long svcStart = System.nanoTime();

            byte[] raw;
            try {
                byte[] iv = Base64.getDecoder().decode(ivB64);
                byte[] ct = Base64.getDecoder().decode(payloadB64);
                raw = aesDecrypt(iv, ct);
            } catch (Exception e) {
                long svcEnd  = System.nanoTime();
                double svcMs = (svcEnd - svcStart) / 1e6;
                double rspMs = (svcEnd - reqStart)  / 1e6;
                logCsv(arrivalNs, svcMs, Math.max(0, rspMs - svcMs), rspMs, 422);
                sendJson(exchange, 422, "{\"error\":\"decrypt_failed\"}");
                return;
            }

            // Unpack IEEE-754 float32 little-endian
            int nSamples = raw.length / 4;
            double[] signal = new double[nSamples];
            ByteBuffer buf = ByteBuffer.wrap(raw).order(ByteOrder.LITTLE_ENDIAN);
            for (int i = 0; i < nSamples; i++) signal[i] = buf.getFloat();

            // FIR convolution
            double[] filtered = firConvolve(signal, KERNEL);

            // Pack back to float32 LE
            ByteBuffer outBuf = ByteBuffer.allocate(nSamples * 4).order(ByteOrder.LITTLE_ENDIAN);
            for (double v : filtered) outBuf.putFloat((float) v);
            byte[] packed = outBuf.array();

            // Re-encrypt
            byte[] respIv  = new byte[16];
            RNG.nextBytes(respIv);
            byte[] encrypted;
            try {
                encrypted = aesEncrypt(respIv, packed);
            } catch (Exception e) {
                sendJson(exchange, 500, "{\"error\":\"encrypt_failed\"}");
                return;
            }

            long   svcEnd  = System.nanoTime();
            double svcMs   = (svcEnd - svcStart) / 1e6;
            double rspMs   = (svcEnd - reqStart)  / 1e6;
            double queueMs = Math.max(0, rspMs - svcMs);
            // ---- END SERVICE WORK ----

            logCsv(arrivalNs, svcMs, queueMs, rspMs, 200);

            String respJson = String.format(
                "{\"ok\":true,\"iv\":\"%s\",\"payload\":\"%s\",\"samples\":%d,\"service_ms\":%.3f}",
                Base64.getEncoder().encodeToString(respIv),
                Base64.getEncoder().encodeToString(encrypted),
                nSamples, svcMs);

            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.getResponseHeaders().set("X-Service-Actual-Ms", String.format("%.3f", svcMs));
            exchange.sendResponseHeaders(200, respJson.length());
            try (OutputStream os = exchange.getResponseBody()) { os.write(respJson.getBytes()); }
        }
    }

    // -----------------------------------------------------------------------
    // DSP
    // -----------------------------------------------------------------------
    static double[] buildHammingFIR(int taps, double cutoff) {
        int half = taps / 2;
        double[] k = new double[taps];
        for (int i = 0; i < taps; i++) {
            int    n    = i - half;
            double sinc = (n == 0) ? 2.0 * cutoff
                                   : Math.sin(2 * Math.PI * cutoff * n) / (Math.PI * n);
            double win  = 0.54 - 0.46 * Math.cos(2 * Math.PI * i / Math.max(1, taps - 1));
            k[i] = sinc * win;
        }
        return k;
    }

    static double[] firConvolve(double[] signal, double[] kernel) {
        int N = signal.length, M = kernel.length;
        double[] out = new double[N];
        for (int i = 0; i < N; i++) {
            double acc = 0;
            for (int j = 0; j < M && j <= i; j++) acc += signal[i - j] * kernel[j];
            out[i] = acc;
        }
        return out;
    }

    // -----------------------------------------------------------------------
    // AES
    // -----------------------------------------------------------------------
    static byte[] aesDecrypt(byte[] iv, byte[] ciphertext) throws Exception {
        Cipher c = Cipher.getInstance("AES/CBC/PKCS5Padding");
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(AES_KEY, "AES"), new IvParameterSpec(iv));
        return c.doFinal(ciphertext);
    }

    static byte[] aesEncrypt(byte[] iv, byte[] plaintext) throws Exception {
        Cipher c = Cipher.getInstance("AES/CBC/PKCS5Padding");
        c.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(AES_KEY, "AES"), new IvParameterSpec(iv));
        return c.doFinal(plaintext);
    }

    // -----------------------------------------------------------------------
    // CSV logging (thread-safe via synchronized)
    // -----------------------------------------------------------------------
    static synchronized void logCsv(long arrivalNs, double svcMs, double queueMs,
                                     double rspMs, int status) {
        if (CSV_FILE.isEmpty()) return;
        try {
            File f = new File(CSV_FILE);
            f.getParentFile().mkdirs();
            boolean writeHeader = CSV_HDR.compareAndSet(false, true) || f.length() == 0;
            try (FileWriter fw = new FileWriter(f, true)) {
                if (writeHeader) fw.write("arrival_unix_ns,service_ms,queue_ms,response_ms,status_code\n");
                fw.write(String.format("%d,%.6f,%.6f,%.6f,%d%n", arrivalNs, svcMs, queueMs, rspMs, status));
            }
        } catch (Exception ignored) {}
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------
    static void sendJson(HttpExchange ex, int status, String body) throws IOException {
        ex.getResponseHeaders().set("Content-Type", "application/json");
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) { os.write(bytes); }
    }

    /** Minimal JSON string extractor — avoids adding a JSON library dependency. */
    static String extractJsonString(String json, String key) {
        String search = "\"" + key + "\"";
        int ki = json.indexOf(search);
        if (ki < 0) throw new IllegalArgumentException("key not found: " + key);
        int colon = json.indexOf(':', ki + search.length());
        int q1    = json.indexOf('"', colon + 1);
        int q2    = json.indexOf('"', q1 + 1);
        return json.substring(q1 + 1, q2);
    }

    static int    intEnv(String k, int    def) { String v = System.getenv(k); return v == null ? def : Integer.parseInt(v); }
    static double dblEnv(String k, double def) { String v = System.getenv(k); return v == null ? def : Double.parseDouble(v); }

    static byte[] parseHexKey(String hex) {
        int len = hex.length() / 2;
        byte[] b = new byte[len];
        for (int i = 0; i < len; i++) b[i] = (byte) Integer.parseInt(hex.substring(i*2, i*2+2), 16);
        return b;
    }
}
