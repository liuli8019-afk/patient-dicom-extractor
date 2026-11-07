# 医院云影像 DICOM 自助提取（患者分享版）
# Self-Service DICOM Retrieval from Hospital Cloud Imaging (Patient Share Edition)

免费开源工具。患者用自己的云影像分享链接，取回检查的原始像素数据并封装成标准 DICOM 文件。
A free, open-source tool. Patients use their own cloud-imaging share link to retrieve the original pixel data of their examination and package it as standard DICOM files.

---

## 一句话用法 / One-line usage

**给你的 AI 助手（Agent）一句话即可运行 / One line for your AI agent:**

```text
git clone https://github.com/liuli8019-afk/patient-dicom-extractor.git 并按其中的 SKILL.md 执行，用我提供的云影像分享链接提取 DICOM，密码由我输入。
```

**自己在电脑上运行，也是一条命令 / One command to run it yourself:**

```bash
git clone https://github.com/liuli8019-afk/patient-dicom-extractor.git && cd patient-dicom-extractor && pip install -r requirements.txt && python3 scripts/tcloud_dicom.py run --viewer-url "你在浏览器输完密码后地址栏里的查看器URL" --out 下载结果
```

（查看器 URL 的获取：打开分享链接 → 输入密码 → 跳转后复制浏览器地址栏里 `TCloudViewer.html?...` 的完整地址。/ To get the viewer URL: open the share link → enter the password → copy the full `TCloudViewer.html?...` address from your browser's address bar after the redirect.）

---

## 中文介绍

### 这是什么

本工具面向上海胸科医院等指定医院的云影像分享链接。这类链接面向患者只提供在线看图，「下载原始 DICOM」的入口在患者分享版里没有开放。本工具用你自己的分享链接和密码，把系统里的原始像素数据取回本地，封装成标准 DICOM 文件，可以导入 3D Slicer、RadiAnt、Horos 等阅片软件，也可以交给你的医生或用于异地会诊。你就诊的医院不在上述范围、或不确定是否适用时，请咨询你的 AI 助手（Agent）：把链接给它，由它打开链接、核对页面与接口结构后现场判断，不要自行套用。

### 先说清三件事

1. **只能取你自己的。** 本工具只使用你本人收到的分享链接、你本人知道的密码、你本人授权的会话。不破解密码、不伪造身份、不访问他人的检查。将自己的链接和密码告诉陌生人有隐私风险，请通过本仓库的公开方法自行操作。
2. **产物是「像素零损失、DICOM 头本地重建」的文件，不是厂商原始文件。** 本方法不去绕过被关闭的官方导出通道，而是通过查看器自身在用的像素接口取回平台保存的原始 16 位像素阵列（有逐字节校验方法），再在本地按 DICOM 标准重建文件头（UID、几何、窗宽窗位均来自平台元数据）。像素完整，但请如实向接收方说明文件来源，不冒充厂商原始导出文件。
3. **影像是敏感医疗数据。** 下载结果只保存在你自己的设备上，不要上传到公共网盘、社交平台，或发送给身份不明的人处理。

### 成功长什么样

一次完整的提取交付应包含：

- 按序列分文件夹的 `.dcm` 文件（每层一个文件）；
- `instance_manifest.csv`（每层文件与平台 ImageUID 的对应关系）、`series_manifest.csv`、`sha256.csv`；
- `verification_report.json` 验证报告，最终状态只能是 **完整 / 部分完成 / 未完成** 三者之一；
- 若缺少验证报告，请按「未完成」对待，并按 `SOP.md` 的验证清单自行复核。

### 适用范围与限制

- 适用：上海胸科医院等指定医院的云影像患者分享链接（`mIdentify.html?...` 形式）。
- 其他医院：不在上述范围的，请咨询你的 Agent，由它打开链接现场判断，不要默认适用。
- 不适用：医护/机构版账号（本身有官方导出，请用官方功能）。
- 系统升级后接口可能变化，届时按 `SOP.md` 的思路重新核对即可。
- 分享链接有有效期，过期请回医院公众号/小程序重新获取。

### 文件说明

- `SOP.md` — 完整技术流程：入口结构、关键接口、签名规则、批量下载、本地封装、验证清单。
- `SKILL.md` — 给 AI Agent 的执行技能文件。
- `scripts/tcloud_dicom.py` — 可直接运行的实现（一条 `run` 命令跑通全流程：建清单 → 下载 → 自检 → 封装 → 验证 → 报告）。分享链接的解密密钥不在代码里写死，按 SOP §3 从平台自己的前端 JS 中还原——那本来就是发到你浏览器里的代码。

### 免责声明

本项目仅供患者取回本人医疗影像数据使用，与任何医院或机构无隶属关系。使用者应遵守所在医院平台的使用条款及当地法律法规。因使用本项目产生的任何问题由使用者自行承担。如平台方认为本项目不当，请通过 Issue 联系，将及时处理。

---

## English Introduction

### What is this?

This tool is for the cloud-imaging share links of designated hospitals, including Shanghai Chest Hospital. These patient-facing links offer online viewing only; an official "download original DICOM" option is not available in that edition. The tool lets you use **your own share link and password** to retrieve the original pixel data stored in the system and package it locally as standard DICOM files, readable by viewers such as 3D Slicer, RadiAnt, and Horos, and suitable for sharing with your own physicians or for remote consultation. If your hospital is not among the designated ones, or you are unsure whether this applies to you, ask your AI agent: give it your link and let it open the link and check the page and interface structure on the spot. Do not assume it applies.

### Three things to know first

1. **Your own images only.** This tool works solely with a share link issued to you, a password you know, and a session authorized by you. It does not crack passwords, forge identities, or access anyone else's examination. Sharing your link and password with strangers is a privacy risk; this public method lets you do it yourself instead.
2. **The output is "pixel-lossless, header rebuilt locally" — not the vendor's original file.** The method does not bypass the disabled official export channel. It retrieves the platform's original 16-bit pixel arrays through the same pixel interface the viewer itself uses (byte-for-byte verifiable), then rebuilds a standard DICOM header locally from platform metadata (UIDs, geometry, window settings). The pixels are complete; please describe the files' origin honestly and do not present them as vendor-exported originals.
3. **Medical images are sensitive data.** Keep downloads on your own devices. Do not upload them to public cloud drives or social platforms, and do not send them to unidentified third parties for processing.

### What a complete result looks like

- One folder per series, containing one `.dcm` file per slice;
- `instance_manifest.csv`, `series_manifest.csv`, and `sha256.csv`;
- A `verification_report.json` whose final status is one of **Complete / Partial / Not completed**. Treat any delivery without a verification report as not completed, and re-check it against the checklist in `SOP.md`.

### Scope and limitations

- **Applies to:** patient cloud-imaging share links (of the form `mIdentify.html?...`) of designated hospitals, including Shanghai Chest Hospital.
- **Other hospitals:** if yours is not listed, ask your AI agent to open your link and check first; do not assume applicability.
- **Not for:** clinician/institution accounts (which have official export — please use it).
- If the system is upgraded, interfaces may change; re-check them following the approach in `SOP.md`.
- Share links expire; if yours has, get a fresh one from the hospital's official app or mini-program.

### Repository contents

- `SOP.md` — the full technical procedure: entry structure, key interfaces, signing rules, batch download, local packaging, and the verification checklist.
- `SKILL.md` — an execution skill file for AI agents.
- `scripts/tcloud_dicom.py` — a runnable implementation: a single `run` command performs inventory → download → sanity check → DICOM packaging → verification → report. The share-link decryption key is not hard-coded; per SOP §3 it is recovered from the platform's own front-end JavaScript — code that is delivered to your browser anyway.

### Disclaimer

This project is provided solely for patients retrieving their own medical imaging data. It is not affiliated with any hospital or institution. Users are responsible for complying with the platform's terms of use and applicable laws and regulations, and for any consequences of using this project. If the platform operator has concerns about this project, please open an Issue and it will be addressed promptly.

## License

MIT
