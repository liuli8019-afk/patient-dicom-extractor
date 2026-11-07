---
name: patient-dicom-extractor
description: 从上海胸科医院等指定医院的云影像患者分享链接提取原始 CT 像素并本地封装为可校验的 Part-10 DICOM。当用户提供指定医院的云影像分享链接（mIdentify.html）并希望下载/导出 DICOM、且患者分享版没有官方导出时使用；其他医院的链接先打开核对入口与接口结构再判断是否适用。只处理用户本人授权的单个 Study。
---

# 指定医院云影像 DICOM 提取

你是执行 Agent，不是顾问。按 SOP.md 全文执行：分析 → 建清单 → 小样本验证 → 批量下载 → 本地封装 → 完整性验证 → 交付。不得只给教程不执行；不得把 JPEG 渲染图冒充 DICOM；不得把「部分完成」说成「完整」。

一条命令入口：`python3 scripts/tcloud_dicom.py run --viewer-url "<用户输完密码后地址栏里的查看器URL>" --out 下载结果`（查看器 URL 由用户在浏览器打开分享链接、输入密码后复制提供；或你已按 §3 还原 AES key 时用 `--share-url <分享链接> --aes-key-hex <hex>`）。`run` 会自动完成建清单、下载、自检、封装、验证与报告；平台字段名与预期不一致时它会保存原始响应并报错，此时按 SOP 手工核对字段映射后重试，不要绕过验证。

## 授权边界（硬约束）

- 仅限用户提供的分享链接对应的单个 Study、当前分享权限可访问的影像。
- 密码由用户本人输入/提供；不解密密码本身，不伪造签名扩大权限，不访问其他患者/Study。
- 官方导出通道（downloadServlet.do）在患者分享版被产品层禁用时，不逆向该通道；使用查看器自身像素接口取回像素，产物如实标注「像素原生、头本地重建」。
- Cookie、Token、签名参数、患者身份信息不得写入公开日志、代码仓库或交付报告。

## 执行流程

1. **入口**：解析分享链接 `mIdentify.html?keyParam&screct&timestap`。从目标站点现行 `js/common.js` 还原 AES-ECB key（两段 base64 混淆，见 SOP §3），解密 keyParam 得到查看器 URL。若站点从当前网络不可达（如云端环境被拒），立即说明并改走用户设备网络，不要反复重试同一路径。
2. **建清单**：GET 查看器 URL 取 cookie → POST `studyInfoServlet.do` 换 sessionID 并保存完整 JSON：全部 Series、每层 ImageUID / Rows / Columns / InstanceNumber / ImagePositionPatient。先落盘 `instance_manifest.csv` 再下载，不凭阅片器可见计数推断总数（阅片器计数只作交叉核对）。
3. **小样本验证**：取目标序列首/中/尾 3 层，POST `dicomImagePixelServlet.do`（签名 `sign=md5(sessionID+imageUID+salt[:32])`，salt 见 SOP §3）。校验：字节数 = Rows×Columns×2；按 RescaleIntercept=−1024 换算后像素分布应有约 −1000 HU 空气峰、高值到约 +1200 HU。不满足则停下报告，不要批量下载。
4. **批量下载**：6 并发；429/5xx 退避重试 ≤4 次；完整字节数才落盘 `.raw`，支持按大小断点续传；每层同步保存响应头 `dicomImageInfo`（URL 编码 JSON）为元数据文件。由 `run` 命令自动执行。
5. **本地封装**：按 SOP §5 用 pydicom 写 Part-10（ExplicitVRLittleEndian / CTImageStorage / 128 字节 preamble）。特别注意：FrameOfReferenceUID 整序列共用一个（uuid5 派生）；SliceThickness/SpacingBetweenSlices 写实测层间距中位数，不写平台名义值；SOPInstanceUID 写入前校验合法性；补齐 BitsAllocated/HighBit/SamplesPerPixel。由 `run` 命令自动执行。
6. **验证**：跑 SOP §6 五项：DICM 魔数+pydicom 解析+像素长度；UID 一致/唯一/与清单对应；相邻层 IPP 差值中位数与离群层；PixelData 与 .raw 逐字节比对；SHA-256 全量记录。由 `run` 命令自动执行，结论写入 `verification_report.json`。
7. **交付**：目录结构按 SOP §7；生成首/中/尾肺窗预览 PNG；`verification_report.json` 的最终状态只能是 完整/部分完成/未完成，并注明「像素原生、头本地重建，非厂商原始文件」。

## 失败处理

- 链接过期/密码错：请用户回医院公众号重新获取链接，不要猜密码。
- 接口返回空：先怀疑签名——用一对已知（密文，签名）本地比对 `getMD5Field` 输出，不要盲试参数后下「不是签名问题」的结论。
- 平台改版：以目标站点现行 JS 与实际请求为准更新 SOP，不要硬套旧参数。
