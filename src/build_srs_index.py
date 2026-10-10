from __future__ import annotations

import argparse
import math
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY", "False")

try:
    import chromadb

    HAS_CHROMADB = True
except Exception:
    HAS_CHROMADB = False

CHROMA_DIR = Path("storage/chroma")
COLLECTION_NAME = "srs_requirements"
EMBED_DIM = 256
BATCH_SIZE = 5000
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass
class SrsRow:
    req_id: str
    section: str
    content: str
    row_num: int


def split_section_and_content(text: str) -> tuple[str, str]:
    match = re.match(r"^(?P<section>\d[\d\.-]*)\s+(?P<body>.+)$", text.strip())
    if not match:
        return "", text.strip()
    return match.group("section"), match.group("body").strip()


def embed_text(text: str, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    for token in text.lower().split():
        vec[hash(token) % dim] += 1.0

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def parse_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []

    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for si in root.findall(f"{{{NS_MAIN}}}si"):
        texts = [t.text or "" for t in si.findall(f".//{{{NS_MAIN}}}t")]
        strings.append("".join(texts))
    return strings


def cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    ctype = cell.attrib.get("t", "")
    if ctype == "s":
        v = cell.find(f"{{{NS_MAIN}}}v")
        if v is None or v.text is None:
            return ""
        idx = int(v.text)
        return shared_strings[idx] if 0 <= idx < len(shared_strings) else ""

    if ctype == "inlineStr":
        t = cell.find(f"{{{NS_MAIN}}}is/{{{NS_MAIN}}}t")
        return t.text if (t is not None and t.text is not None) else ""

    v = cell.find(f"{{{NS_MAIN}}}v")
    return v.text if (v is not None and v.text is not None) else ""


def resolve_sheet_path(zf: zipfile.ZipFile, worksheet_name: str) -> tuple[str, str]:
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))

    sheets = wb.find(f"{{{NS_MAIN}}}sheets")
    if sheets is None:
        raise ValueError("Invalid workbook: missing sheets")

    target_sheet = None
    if worksheet_name:
        for sh in sheets.findall(f"{{{NS_MAIN}}}sheet"):
            if sh.attrib.get("name", "") == worksheet_name:
                target_sheet = sh
                break
        if target_sheet is None:
            raise ValueError(f"Worksheet not found: {worksheet_name}")
    else:
        target_sheet = sheets.findall(f"{{{NS_MAIN}}}sheet")[0]

    rid = target_sheet.attrib.get(f"{{{NS_REL}}}id", "")
    if not rid:
        raise ValueError("Worksheet relationship id not found")

    target = ""
    for rel in rels.findall(f"{{{NS_PKG_REL}}}Relationship"):
        if rel.attrib.get("Id", "") == rid:
            target = rel.attrib.get("Target", "")
            break

    if not target:
        raise ValueError("Worksheet target not found in rels")

    sheet_name = target_sheet.attrib.get("name", "")
    if target.startswith("/"):
        return sheet_name, f"xl{target}"
    return sheet_name, f"xl/{target}"


def extract_srs_rows(xlsx_path: Path, worksheet_name: str) -> tuple[str, list[SrsRow]]:
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        shared = parse_shared_strings(zf)
        selected_sheet_name, sheet_path = resolve_sheet_path(zf, worksheet_name)
        root = ET.fromstring(zf.read(sheet_path))

    rows: list[SrsRow] = []
    sheet_data = root.find(f"{{{NS_MAIN}}}sheetData")
    if sheet_data is None:
        return selected_sheet_name, rows

    for row in sheet_data.findall(f"{{{NS_MAIN}}}row"):
        row_num = int(row.attrib.get("r", "0") or "0")
        id_text = ""
        content_text = ""

        for cell in row.findall(f"{{{NS_MAIN}}}c"):
            ref = cell.attrib.get("r", "")
            if ref.startswith("A"):
                id_text = cell_text(cell, shared).strip()
            elif ref.startswith("B"):
                content_text = cell_text(cell, shared).strip()

        if id_text and content_text:
            section, content = split_section_and_content(content_text)
            rows.append(SrsRow(req_id=id_text, section=section, content=content, row_num=row_num))

    return selected_sheet_name, rows


def build_srs_index(xlsx_path: Path, worksheet_name: str, rebuild: bool) -> int:
    if not xlsx_path.exists():
        raise FileNotFoundError(f"SRS file not found: {xlsx_path}")

    print(f"Reading SRS workbook: {xlsx_path}")
    used_sheet_name, srs_rows = extract_srs_rows(xlsx_path, worksheet_name)
    if not srs_rows:
        print("No valid rows found (A=id, B=content).")
        return 0
    print(f"Loaded {len(srs_rows)} rows from sheet '{used_sheet_name}'.")

    if not HAS_CHROMADB:
        raise RuntimeError("chromadb is not installed. Please install dependencies first.")

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Opening Chroma database: {CHROMA_DIR}")
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    if rebuild:
        existing = [c.name for c in client.list_collections()]
        if COLLECTION_NAME in existing:
            print(f"Deleting existing collection: {COLLECTION_NAME}")
            client.delete_collection(COLLECTION_NAME)

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict[str, str | int]] = []
    embs: list[list[float]] = []

    source_path = xlsx_path.as_posix()
    print("Preparing local embeddings...")
    for idx, row in enumerate(srs_rows, start=1):
        ids.append(f"{row.req_id}")
        docs.append(row.content)
        metas.append(
            {
                "req_id": row.req_id,
                "section": row.section,
                "source": source_path,
                "sheet": used_sheet_name,
                "row": row.row_num,
            }
        )
        embs.append(embed_text(row.content))
        if idx == len(srs_rows) or idx % BATCH_SIZE == 0:
            percent = idx * 100 / len(srs_rows)
            print(f"  embedded {idx}/{len(srs_rows)} ({percent:.1f}%)", flush=True)

    print(f"Upserting to Chroma in batches of {BATCH_SIZE}...")
    for start in range(0, len(ids), BATCH_SIZE):
        end = min(start + BATCH_SIZE, len(ids))
        collection.upsert(
            ids=ids[start:end],
            documents=docs[start:end],
            metadatas=metas[start:end],
            embeddings=embs[start:end],
        )
        percent = end * 100 / len(ids)
        print(f"  upserted {end}/{len(ids)} ({percent:.1f}%)", flush=True)

    print(
        f"Indexed {len(docs)} SRS requirements into collection '{COLLECTION_NAME}' from sheet '{used_sheet_name}'."
    )
    return len(docs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build vector index from preprocessed SRS xlsx")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("docs/SRS/SRS_baseline_25.76(VM14.0 SVAL-R)_preprocessed.xlsx"),
        help="Path to preprocessed xlsx",
    )
    parser.add_argument("--sheet", type=str, default="Sheet1", help="Worksheet name")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild collection")
    args = parser.parse_args()

    build_srs_index(xlsx_path=args.input, worksheet_name=args.sheet, rebuild=args.rebuild)
    os._exit(0)


if __name__ == "__main__":
    main()
