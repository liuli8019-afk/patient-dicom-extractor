#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Winning TCloud 患者分享版 DICOM 提取（一条命令跑通版）。

最简用法（打开分享链接、输入密码后，把浏览器地址栏里的查看器 URL 复制出来）：

  python3 tcloud_dicom.py run --viewer-url "<TCloudViewer.html?...>" --out 下载结果

Agent 已按 SOP §3 还原 AES key 时，也可以直接给分享链接：

  python3 tcloud_dicom.py run --share-url "<mIdentify.html?...>" --aes-key-hex <hex> --out 下载结果

run 全流程：studyInfo 建清单 → 像素批量下载 → 首/中/尾分布自检 → 本地封装
Part-10 DICOM → 五项验证 → 写 manifest / sha256 / verification_report.json。
平台返回 JSON 的字段名在不同部署间可能有差异，本脚本做容错映射；映射失败时
会保存 studyInfo_raw.json 并明确报错，不会假装成功。

签名规则（平台前端同款）：sign = md5(sessionID + imageUID + SALT32)
仅用于本人授权的分享会话。产物为「像素原生、头本地重建」，非厂商原始文件。
"""
import argparse, csv, hashlib, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SALT32 = "WinningHealthTechnologyGroupCo.,"  # 完整盐串取前 32 位，见 SOP §3
CT_IMAGE_STORAGE = "1.2.840.10008.5.1.4.1.1.2"

# ---------------- 通用工具 ----------------

def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def sign_pixel(session_id: str, image_uid: str) -> str:
    return md5_hex(session_id + image_uid + SALT32)

def is_valid_uid(uid: str) -> bool:
    if not uid or len(uid) > 64:
        return False
    if not re.fullmatch(r"[0-9.]+", uid):
        return False
    return all(p == "0" or not p.startswith("0") for p in uid.split("."))

def uid_from_uuid5(name: str) -> str:
    import uuid
    return "2.25." + str(uuid.uuid5(uuid.NAMESPACE_URL, name).int)

def clean_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(s)).strip("_") or "Series"

def _pick(d: dict, *names):
    """大小写不敏感地从 dict 里按候选名取值。"""
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low and low[n.lower()] not in (None, ""):
            return low[n.lower()]
    return None

def _floats(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return [float(x) for x in v]
    return [float(x) for x in re.split(r"[\\,\s]+", str(v).strip()) if x]

# ---------------- 1) 分享链接解密（可选入口） ----------------

def decrypt_share_link(share_url: str, aes_key: bytes) -> str:
    """AES-ECB/Pkcs7 解密 keyParam，返回查看器 URL。key 按 SOP §3 从 common.js 还原。"""
    from urllib.parse import urlparse, parse_qs
    from base64 import b64decode
    from Crypto.Cipher import AES  # pycryptodome，仅本步骤需要
    q = parse_qs(urlparse(share_url).query)
    enc = q["keyParam"][0]
    raw = AES.new(aes_key, AES.MODE_ECB).decrypt(b64decode(enc))
    pad = raw[-1]
    return raw[:-pad].decode("utf-8")

# ---------------- 2) studyInfo 容错解析 ----------------

def parse_study_info(doc) -> tuple:
    """返回 (session_id, study_uid, series_list)。
    series_list: [{series_number, series_description, series_uid,
                   instances: [{image_uid, instance_number, rows, cols, ipp, iop,
                                pixel_spacing, window_center, window_width, modality}]}]
    """
    def walk_find_key(obj, wanted, depth=0):
        if depth > 6:
            return None
        if isinstance(obj, dict):
            v = _pick(obj, *wanted)
            if v:
                return v
            for val in obj.values():
                r = walk_find_key(val, wanted, depth + 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for val in obj:
                r = walk_find_key(val, wanted, depth + 1)
                if r:
                    return r
        return None

    session_id = walk_find_key(doc, ["sessionID", "sessionId", "session_id"])
    study_uid = walk_find_key(doc, ["studyInstanceUID", "studyUID", "studyInstanceUid"])

    def find_series_lists(obj, out, depth=0):
        if depth > 6:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, list) and v and isinstance(v[0], dict) and \
                        _pick(v[0], "seriesInstanceUID", "seriesUID", "seriesNumber") is not None:
                    out.append(v)
                else:
                    find_series_lists(v, out, depth + 1)
        elif isinstance(obj, list):
            for val in obj:
                find_series_lists(val, out, depth + 1)

    lists = []
    find_series_lists(doc, lists)
    if not lists:
        raise ValueError("studyInfo 中未找到 Series 列表（原始响应已保存，请按其结构调整映射）")
    series_list = []
    for s in lists[0]:
        inst_raw = _pick(s, "instances", "instanceList", "images", "imageList", "instanceInfos") or []
        instances = []
        for im in inst_raw:
            instances.append({
                "image_uid": str(_pick(im, "imageUID", "ImageUID", "imageUid", "uid",
                                       "SOPInstanceUID", "sopInstanceUID")),
                "instance_number": int(_pick(im, "instanceNumber", "InstanceNumber", "number") or 0),
                "rows": int(_pick(im, "rows", "Rows", "height") or 0),
                "cols": int(_pick(im, "columns", "Columns", "cols", "width") or 0),
                "ipp": _floats(_pick(im, "imagePositionPatient", "ImagePositionPatient", "ipp")),
                "iop": _floats(_pick(im, "imageOrientationPatient", "ImageOrientationPatient", "iop"))
                       or [1, 0, 0, 0, 1, 0],
                "pixel_spacing": _floats(_pick(im, "pixelSpacing", "PixelSpacing")) or [0.7, 0.7],
                "window_center": _pick(im, "windowCenter", "WindowCenter"),
                "window_width": _pick(im, "windowWidth", "WindowWidth"),
                "modality": _pick(im, "modality", "Modality") or "CT",
            })
        series_list.append({
            "series_number": int(_pick(s, "seriesNumber", "SeriesNumber") or 0),
            "series_description": _pick(s, "seriesDescription", "SeriesDescription") or "",
            "series_uid": str(_pick(s, "seriesInstanceUID", "seriesUID", "seriesInstanceUid") or ""),
            "instances": sorted(instances, key=lambda x: x["instance_number"]),
        })
    return session_id, study_uid, series_list

# ---------------- 3) 像素下载 ----------------

def download_pixels(base: str, session_id: str, instances: list, raw_dir: Path,
                    session=None, workers: int = 6) -> dict:
    import requests
    sess = session or requests.Session()
    raw_dir.mkdir(parents=True, exist_ok=True)
    results = {}

    def one(inst):
        uid = inst["image_uid"]
        expect = int(inst["rows"]) * int(inst["cols"]) * 2
        dest = raw_dir / f"{uid}.raw"
        meta = raw_dir / f"{uid}.json"
        if dest.exists() and dest.stat().st_size == expect:
            return uid, "skip_ok", expect
        last_err = None
        for attempt in range(4):
            try:
                resp = sess.post(base.rstrip("/") + "/dicomImagePixelServlet.do",
                                 data={"sessionID": session_id, "imageUID": uid,
                                       "sign": sign_pixel(session_id, uid)}, timeout=60)
                resp.raise_for_status()
                data = resp.content
                if len(data) != expect:
                    raise ValueError(f"size {len(data)} != expect {expect}")
                dest.write_bytes(data)
                info = resp.headers.get("dicomImageInfo")
                if info:
                    from urllib.parse import unquote
                    meta.write_text(unquote(info), encoding="utf-8")
                return uid, "ok", expect
            except Exception as e:
                last_err = e
                time.sleep(min(2 ** attempt, 8))
        return uid, f"failed: {last_err}", 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(one, i) for i in instances]):
            uid, status, size = fut.result()
            results[uid] = {"status": status, "bytes": size}
    return results

def check_representative(raw_dir: Path, sample_uids: list) -> dict:
    """首/中/尾三层像素分布 sanity check：应有约 -1000 HU 空气峰。"""
    import numpy as np
    out = {}
    for uid in sample_uids:
        px = np.frombuffer((raw_dir / f"{uid}.raw").read_bytes(), dtype="<u2").astype(int) - 1024
        out[uid] = {"min": int(px.min()), "max": int(px.max()),
                    "frac_near_air": round(float(((px > -1050) & (px < -950)).mean()), 4)}
    return out

# ---------------- 4) 本地封装 Part-10 DICOM ----------------

def encapsulate_series(instances: list, raw_dir: Path, out_dir: Path,
                       study_uid: str, series_uid: str, series_desc: str = "") -> list:
    import pydicom
    import numpy as np
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian

    out_dir.mkdir(parents=True, exist_ok=True)
    instances = sorted(instances, key=lambda x: int(x["instance_number"]))
    spacing = None
    if len(instances) > 1 and all(i.get("ipp") for i in instances):
        iop = np.array(instances[0]["iop"], dtype=float)
        normal = np.cross(iop[:3], iop[3:])  # 层法向量 = 行方向 × 列方向
        ipps = [np.array(i["ipp"], dtype=float) for i in instances]
        diffs = [abs(float(np.dot(ipps[k + 1] - ipps[k], normal))) for k in range(len(ipps) - 1)]
        diffs = [d for d in diffs if d > 0]
        spacing = float(np.median(diffs)) if diffs else None
    for_uid = uid_from_uuid5(series_uid)  # 整序列共用一个坐标系，见 SOP §5
    written = []
    for inst in instances:
        uid_raw = inst["image_uid"]
        sop_uid = uid_raw if is_valid_uid(uid_raw) else uid_from_uuid5(uid_raw)
        fm = FileMetaDataset()
        fm.MediaStorageSOPClassUID = CT_IMAGE_STORAGE
        fm.MediaStorageSOPInstanceUID = sop_uid
        fm.TransferSyntaxUID = ExplicitVRLittleEndian
        fm.ImplementationClassUID = uid_from_uuid5("tcloud-extractor")
        ds = Dataset()
        ds.file_meta = fm
        ds.SOPClassUID = CT_IMAGE_STORAGE
        ds.SOPInstanceUID = sop_uid
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = for_uid
        ds.Modality = inst.get("modality", "CT")
        if series_desc:
            ds.SeriesDescription = series_desc
            if any(ord(c) > 127 for c in series_desc):
                ds.SpecificCharacterSet = "GB18030"  # 中文序列名防乱码
        ds.InstanceNumber = int(inst["instance_number"])
        ds.ImagePositionPatient = [float(v) for v in inst["ipp"]]
        ds.ImageOrientationPatient = [float(v) for v in inst["iop"]]
        ds.PixelSpacing = [float(v) for v in inst["pixel_spacing"]]
        if spacing:
            ds.SliceThickness = round(spacing, 4)
            ds.SpacingBetweenSlices = round(spacing, 4)
        ds.Rows = int(inst["rows"]); ds.Columns = int(inst["cols"])
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16; ds.BitsStored = 12; ds.HighBit = 11
        ds.PixelRepresentation = 0
        ds.RescaleIntercept = -1024; ds.RescaleSlope = 1
        if inst.get("window_center"):
            ds.WindowCenter = float(str(inst["window_center"]).split("/")[0].split("\\")[0])
        if inst.get("window_width"):
            ds.WindowWidth = float(str(inst["window_width"]).split("/")[0].split("\\")[0])
        ds.PixelData = (raw_dir / f"{uid_raw}.raw").read_bytes()
        name = f"{int(inst['instance_number']):05d}.dcm"
        pydicom.dcmwrite(out_dir / name, ds, enforce_file_format=True)
        written.append({"file": name, "sop_uid": sop_uid, "image_uid": uid_raw,
                        "instance_number": int(inst["instance_number"]),
                        "slice_location": inst["ipp"][2] if inst.get("ipp") else None,
                        "bytes": (out_dir / name).stat().st_size})
    return written

# ---------------- 5) 验证 ----------------

def verify_series(dcm_dir: Path, raw_dir: Path, study_uid: str, series_uid: str,
                  raw_name_by_sop: dict = None) -> dict:
    import pydicom
    files = sorted(dcm_dir.glob("*.dcm"))
    seen_sop, problems, sha_rows, inst_nums, z_positions = set(), [], [], [], []
    for f in files:
        data = f.read_bytes()
        sha_rows.append({"file": f.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        if len(data) < 132 or data[128:132] != b"DICM":
            problems.append(f"{f.name}: missing DICM magic"); continue
        try:
            ds = pydicom.dcmread(f)
        except Exception as e:
            problems.append(f"{f.name}: pydicom parse failed: {e}"); continue
        if ds.StudyInstanceUID != study_uid or ds.SeriesInstanceUID != series_uid:
            problems.append(f"{f.name}: Study/Series UID mismatch")
        if ds.SOPInstanceUID in seen_sop:
            problems.append(f"{f.name}: duplicate SOPInstanceUID")
        seen_sop.add(ds.SOPInstanceUID)
        inst_nums.append(int(ds.InstanceNumber))
        if len(ds.PixelData) != int(ds.Rows) * int(ds.Columns) * 2:
            problems.append(f"{f.name}: pixel length mismatch")
        raw_name = (raw_name_by_sop or {}).get(ds.SOPInstanceUID, ds.SOPInstanceUID)
        raw = raw_dir / f"{raw_name}.raw"
        if raw.exists() and raw.read_bytes() != ds.PixelData:
            problems.append(f"{f.name}: PixelData != platform .raw")
        try:
            z_positions.append(float(ds.ImagePositionPatient[2]))
        except Exception:
            pass
    inst_nums.sort()
    gaps = [n for n in range(inst_nums[0], inst_nums[-1] + 1) if n not in inst_nums] if inst_nums else []
    if gaps:
        problems.append(f"InstanceNumber gaps: {gaps}")
    return {"files": len(files), "parsed_ok": len(seen_sop), "problems": problems,
            "instance_gaps": gaps, "sha256": sha_rows,
            "z_span": (max(z_positions) - min(z_positions)) if len(z_positions) > 1 else None,
            "status": "完整" if not problems else "部分完成"}

# ---------------- 6) run 全流程 ----------------

def run_pipeline(viewer_url: str, out_root: Path, only_series: list = None,
                 workers: int = 6, base_override: str = None) -> dict:
    import requests
    from urllib.parse import urlparse, parse_qs
    u = urlparse(viewer_url)
    base = base_override or f"{u.scheme}://{u.netloc}{u.path.rsplit('/', 1)[0]}"
    query = {k: v[0] for k, v in parse_qs(u.query).items()}
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    sess.get(viewer_url, timeout=30)  # 拿 cookie
    r = sess.post(base.rstrip("/") + "/studyInfoServlet.do", data=query, timeout=30)
    r.raise_for_status()
    info_dir = out_root / "Study_信息"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "studyInfo_raw.json").write_text(r.text, encoding="utf-8")
    session_id, study_uid, series_list = parse_study_info(r.json())
    if not session_id:
        raise ValueError("studyInfo 未返回 sessionID（原始响应已保存到 Study_信息/studyInfo_raw.json）")
    report = {"study_uid": study_uid, "note": "像素原生、头本地重建，非厂商原始文件",
              "series": [], "status": "完整"}
    series_rows, instance_rows, sha_all = [], [], []
    for s in series_list:
        if only_series and s["series_number"] not in only_series:
            continue
        sname = f"Series_{s['series_number']:03d}_{clean_name(s['series_description'])}"
        raw_dir = out_root / ".raw" / sname
        dcm_dir = out_root / sname
        dl = download_pixels(base, session_id, s["instances"], raw_dir, session=sess, workers=workers)
        failed = [k for k, v in dl.items() if v["status"] == "failed" or v["status"].startswith("failed")]
        ok_uids = [i["image_uid"] for i in s["instances"] if i["image_uid"] not in failed]
        if len(ok_uids) >= 3:
            sample = [ok_uids[0], ok_uids[len(ok_uids) // 2], ok_uids[-1]]
            chk = check_representative(raw_dir, sample)
        else:
            chk = {}
        ok_insts = [i for i in s["instances"] if i["image_uid"] in ok_uids]
        written = encapsulate_series(ok_insts, raw_dir, dcm_dir, study_uid,
                                     s["series_uid"], s["series_description"]) if ok_insts else []
        raw_by_sop = {w["sop_uid"]: w["image_uid"] for w in written}
        ver = verify_series(dcm_dir, raw_dir, study_uid, s["series_uid"], raw_by_sop) if written \
            else {"files": 0, "parsed_ok": 0, "problems": ["no files"], "sha256": [], "status": "未完成"}
        if failed:
            ver["problems"].append(f"download failed: {failed}")
            ver["status"] = "部分完成"
        for w in written:
            instance_rows.append({"series_number": s["series_number"], **w})
        sha_all += [{"series": sname, **row} for row in ver["sha256"]]
        series_rows.append({"series_number": s["series_number"],
                            "series_description": s["series_description"],
                            "target": len(s["instances"]), "downloaded": len(ok_uids),
                            "verified": ver["parsed_ok"], "status": ver["status"]})
        report["series"].append({"series": sname, "representative_check": chk,
                                 "files": ver["files"], "parsed_ok": ver["parsed_ok"],
                                 "problems": ver["problems"], "status": ver["status"]})
        if ver["status"] != "完整":
            report["status"] = "部分完成"
    with open(info_dir / "series_manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["series_number", "series_description", "target",
                                          "downloaded", "verified", "status"])
        w.writeheader(); w.writerows(series_rows)
    with open(info_dir / "instance_manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["series_number", "file", "sop_uid", "image_uid",
                                          "instance_number", "slice_location", "bytes"])
        w.writeheader(); w.writerows(instance_rows)
    with open(info_dir / "sha256.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["series", "file", "sha256", "bytes"])
        w.writeheader(); w.writerows(sha_all)
    (info_dir / "verification_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report

# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser(description="Winning TCloud DICOM 提取（见 SOP.md）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decrypt-link")
    d.add_argument("--share-url", required=True)
    d.add_argument("--aes-key-hex", required=True)
    rn = sub.add_parser("run")
    rn.add_argument("--viewer-url", help="输入密码后浏览器地址栏里的 TCloudViewer.html 完整 URL")
    rn.add_argument("--share-url", help="原始分享链接（需同时给 --aes-key-hex）")
    rn.add_argument("--aes-key-hex")
    rn.add_argument("--series", type=int, action="append", help="只下指定 SeriesNumber，可重复；缺省全部")
    rn.add_argument("--out", default="下载结果")
    rn.add_argument("--workers", type=int, default=6)
    rn.add_argument("--base-url", help="接口基地址覆盖（一般不需要；测试用）")
    args = ap.parse_args()
    if args.cmd == "decrypt-link":
        print(decrypt_share_link(args.share_url, bytes.fromhex(args.aes_key_hex)))
        return
    viewer = args.viewer_url
    if not viewer:
        if not (args.share_url and args.aes_key_hex):
            ap.error("run 需要 --viewer-url，或 --share-url + --aes-key-hex")
        viewer = decrypt_share_link(args.share_url, bytes.fromhex(args.aes_key_hex))
    report = run_pipeline(viewer, Path(args.out), only_series=args.series,
                          workers=args.workers, base_override=args.base_url)
    print(json.dumps({"status": report["status"], "series": len(report["series"])},
                     ensure_ascii=False))
    print(f"交付目录：{Path(args.out).resolve()}（含 Study_信息/verification_report.json）")

if __name__ == "__main__":
    main()
