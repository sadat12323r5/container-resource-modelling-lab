from pypdf import PdfReader

pdf_path = 'Thesis_A_Seminar.pdf'
out_path = 'Thesis_A_Seminar.txt'

reader = PdfReader(pdf_path)
texts = []
for i, page in enumerate(reader.pages, start=1):
    t = page.extract_text() or ''
    texts.append(t)

text = '\n\n'.join(texts)
with open(out_path, 'w', encoding='utf-8') as f:
    f.write(text)
print(f'Wrote {out_path}')
