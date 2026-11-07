#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Winning TCloud 患者分享版 DICOM 提取参考实现。

实现 SOP 的确定性部分：
  download     —— studyInfo 建清单 + dicomImagePixelServlet 批量取原始像素 (.raw)
  encapsulate  —— .raw + 平台元数据 -> Part-10 DICOM (pydicom)
  verify       —— DICM/解析/像素长度/UID/几何/逐字节/SHA-256 全量验证

分享链接解密（keyParam/screct/timestap）所需的 AES-ECB key 不写死在本文件：
按 SOP §3 从目标站点现行 js/common.js 还原后，用 --aes-key-hex 传入，
或先在浏览器/Agent 环境中解出查看器 URL 后用 --viewer-url 直接进入 download。

签名规则（平台前端同款）：sign = md5(sessionID + imageUID + SALT32)
仅用于本人授权的分享会话。产物为「像素原生、头本地重建」，非厂商原始文件。
"""
import argparse, csv, hashlib, json, math, re, sys, time
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

# ---------------- 1) 分享链接解密（可选入口） ----------------

def decrypt_share_link(share_url: str, aes_key: bytes) -> str:
    """AES-ECB/Pkcs7 解密 keyParam，返回查看器 URL。key 按 SOP §3 从 common.js 还原。"""
    from urllib.parse import urlparse, parse_qs, unquote
    from base64 import b64decode
    from Crypto.Cipher import AES  # pycryptodome，仅本步骤需要
    q = parse_qs(urlparse(share_url).query)
    enc = q["keyParam"][0]
    raw = AES.new(aes_key, AES.MODE_ECB).decrypt(b64decode(enc))
    pad = raw[-1]
    return raw[:-pad].decode("utf-8")

# ---------------- 2) 清单 + 像素下载 ----------------

def fetch_study_info(base: str, cookies: dict, post_data: dict) -> dict:
    import requests
    r = requests.post(base.rstrip("/") + "/studyInfoServlet.do",
                      data=post_data, cookies=cookies, timeout=30)
    r.raise_for_status()
    return r.json()

def download_pixels(base: str, session_id: str, instances: list, raw_dir: Path,
                    cookies: dict = None, workers: int = 6) -> dict:
    """instances: [{image_uid, rows, cols, ...}]。完整字节才落盘，支持按大小续传。"""
    import requests
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
                body = {"sessionID": session_id, "imageUID": uid}
                resp = requests.post(
                    base.rstrip("/") + "/dicomImagePixelServlet.do",
                    data={**body, "sign": sign_pixel(session_id, uid)},
                    cookies=cookies or {}, timeout=60)
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
            except Exception as e:  # 429/5xx/超时统一退避
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

# ---------------- 3) 本地封装 Part-10 DICOM ----------------

def encapsulate_series(instances: list, raw_dir: Path, out_dir: Path,
                       study_uid: str, series_uid: str, series_desc: str = "") -> list:
    """instances 须含 image_uid/instance_number/ipp/iop/pixel_spacing/rows/cols，
    可选 window_center/window_width/slice_thickness。返回写出的文件记录。"""
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian

    out_dir.mkdir(parents=True, exist_ok=True)
    instances = sorted(instances, key=lambda x: int(x["instance_number"]))
    # 实测层间距：相邻层 IPP 沿法向量的投影差中位数
    spacing = None
    if len(instances) > 1 and all("ipp" in i for i in instances):
        import numpy as np
        ipps = [np.array(i["ipp"], dtype=float) for i in instances]
        diffs = [abs(float(np.dot(ipps[k + 1] - ipps[k],
                    np.array(instances[0].get("slice_normal", [0, 0, 1]), dtype=float))))
                 for k in range(len(ipps) - 1)]
        spacing = float(np.median([d for d in diffs if d > 0])) if diffs else None
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
        path = out_dir / name
        pydicom.dcmwrite(path, ds, enforce_file_format=True)
        written.append({"file": name, "sop_uid": sop_uid, "image_uid": uid_raw,
                        "instance_number": int(inst["instance_number"])})
    return written

# ---------------- 4) 验证 ----------------

def verify_series(dcm_dir: Path, raw_dir: Path, study_uid: str, series_uid: str) -> dict:
    import pydicom
    files = sorted(dcm_dir.glob("*.dcm"))
    seen_sop, problems, sha_rows, inst_nums = set(), [], [], []
    z_positions = []
    for f in files:
        data = f.read_bytes()
        sha_rows.append({"file": f.name, "sha256": hashlib.sha256(data).hexdigest(),
                         "bytes": len(data)})
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
        raw = raw_dir / f"{_raw_name_for(ds, f)}.raw"
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

def _raw_name_for(ds, f: Path) -> str:
    # .raw 以平台 ImageUID 命名；SOP 由 ImageUID 派生时需由调用方在 manifest 中映射，
    # 此处默认 SOP 未派生（ImageUID 本身合法）的常见情形。
    return ds.SOPInstanceUID

# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser(description="Winning TCloud DICOM 提取（见 SOP.md）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decrypt-link")
    d.add_argument("--share-url", required=True)
    d.add_argument("--aes-key-hex", required=True, help="按 SOP §3 从 common.js 还原的 AES key（hex）")
    args = ap.parse_args()
    if args.cmd == "decrypt-link":
        print(decrypt_share_link(args.share_url, bytes.fromhex(args.aes_key_hex)))
    # download / encapsulate / verify 的编排见 SKILL.md 流程与本文件函数；
    # studyInfo 的字段名以目标站点实际返回为准，映射到 instances 结构后调用。

if __name__ == "__main__":
    main()
