#!/usr/bin/env python3
"""대시보드 저장소 문서를 대시보드 파일(data/c/<corp_code>.json, data/l/<검색키>.json)로 만든다.

저장소(db)를 못 쓰는 화면(claude.ai 밖, 다른 조직 계정)에서도 저장된 기업을 보이게 하려는 것.

사용법:
    python3 scripts/static_files.py .cache/upload/writes.json        # 방금 수집한 것만
    python3 scripts/static_files.py --export .cache/export            # ArtifactData list(out_dir)로 받은 전체

출력: .cache/static/ 아래 파일들과, Artifact publish의 `files`에 그대로 넣을 매핑 .cache/static/files.json
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, ".cache", "static")
FOLDER = {"companies": "c", "lookup": "l"}


def body(path):
    d = json.load(open(path))
    return d["data"] if isinstance(d.get("data"), dict) and "id" in d else d   # {id, data, version}로 저장된 경우


def main(argv):
    docs = []   # (collection, doc_id, source path)
    if argv[:1] == ["--export"]:
        for coll in FOLDER:
            folder = os.path.join(argv[1], coll)
            for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
                if name.endswith(".json"):
                    docs.append((coll, name[:-5], os.path.join(folder, name)))
    else:
        docs = [(w["collection"], w["doc_id"], w["file_path"]) for w in json.load(open(argv[0])) if w["collection"] in FOLDER]
    files = {}
    for coll, doc_id, src in docs:
        rel = f"data/{FOLDER[coll]}/{doc_id}.json"
        dst = os.path.join(OUT, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w") as fh:
            json.dump(body(src), fh, ensure_ascii=False, separators=(",", ":"))
        files[rel] = dst
    with open(os.path.join(OUT, "files.json"), "w") as fh:
        json.dump(files, fh, ensure_ascii=False, indent=1)
    print(f"대시보드 파일 {len(files)}개 → {os.path.join(OUT, 'files.json')}")


if __name__ == "__main__":
    main(sys.argv[1:] or sys.exit(__doc__))
