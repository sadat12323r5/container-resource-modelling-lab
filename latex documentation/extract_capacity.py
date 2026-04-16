from pypdf import PdfReader
pdf_path = 'Thesis A_ Capacity planning on docker containers.pdf'
out_path = 'Thesis_A_Capacity_planning.txt'
reader = PdfReader(pdf_path)
texts = []
for page in reader.pages:
    texts.append(page.extract_text() or '')
text = '\n\n'.join(texts)
with open(out_path, 'w', encoding='utf-8') as f:
    f.write(text)
print('Wrote', out_path)
