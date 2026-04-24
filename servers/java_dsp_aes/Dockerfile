FROM eclipse-temurin:21-jdk-alpine AS builder
WORKDIR /app
COPY DspServer.java .
RUN javac DspServer.java

FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
COPY --from=builder /app/*.class .
EXPOSE 3000
CMD ["java", "DspServer"]
