# 医院云影像 DICOM 提取 SOP（患者分享版 · Winning TCloud）

适用：Winning Health TCloud 患者分享链接（如上海胸科医院 `yyx.shchest.cn`）。
目标：在患者分享版禁用「原始 DICOM 导出」的前提下，拿到平台原始 16 位 CT 像素阵列，本地封装成可校验、可导入阅片软件的 Part-10 DICOM。

**授权边界**：只使用分享链接本人授权的会话；不解密密码、不伪造签名去扩大权限；患者分享版产品层禁用 DICOM 导出时，不强行绕过被禁用的导出通道。本 SOP 产出的是「像素零损失、DICOM 头本地重建」的文件，不是厂商原始文件，对外必须如实标注。

> 发布版修订说明（与原始内部版的差异，均为补全而非改向）：
> 1. §5 补齐 `BitsAllocated=16`、`HighBit=11`、`SamplesPerPixel=1`——只写 BitsStored 时部分阅片软件拒读；
> 2. §5 增加 SOPInstanceUID 合法性校验：平台 ImageUID 不是合法 DICOM UID 时必须派生并记录映射，不得直接写入；
> 3. 文中所有具体数字（层数、层间距、Z 跨度等）均为**示例病例的技术参数（已脱敏）**，实际执行以自己 Study 的 studyInfo 清单为准。

## 1 入口结构

分享链接形如 `mIdentify.html?keyParam=…&screct=…&timestap=…`。这是纯前端密码页：

- `screct`：AES(ECB/Pkcs7) 加密的密码，页面内本地比对，服务端无校验；
- `keyParam`：AES 加密后的真实查看器 URL；
- `timestap`：AES 加密的过期时间戳（epoch ms）。

密码正确后前端跳转到 `TCloudViewer.html?studyUIDs=<base64>&hospcode=<base64>&sign=<32位hex>&shareTag=<base64>`。**密码由用户手动输入，Agent 不代为解密密码本身。**

## 2 关键接口

| 接口 | 用途 | 返回 |
|---|---|---|
| `studyInfoServlet.do`（POST） | 换 sessionID，拿全部 Series / 每层 ImageUID、Rows、Columns、InstanceNumber、ImagePositionPatient | JSON |
| `dicomImagePixelServlet.do`（POST） | 取原始像素：长度必须等于 Rows×Columns×2；响应头 `dicomImageInfo` 是 URL 编码的 JSON 元数据 | 示例：512×512×2 = 524,288 字节 |
| `dicomImageServlet.do`（GET） | 渲染图（512×512 JPEG，8 bit 已压窗），**不能**用于 DICOM 提取 | JPEG |
| `downloadServlet.do` | 官方 DICOM 导出；患者分享版被前端 `display:none` 禁用，点确认抛 undefined、零请求，不要继续逆向这条通道 | — |

## 3 加密与签名

规则来自平台自己的 `js/common.js` 与 `tcloudviewer.js`，直接复用，不要自创：

- AES-ECB key 由 `common.js` 中两段 base64 混淆后还原，用于 `keyParam` / `screct` / `timestap` 的解密；仓库代码不写死该 key，执行时从目标站点现行的 `common.js` 还原（平台改版时以现行 JS 为准）；
- `getMD5Field(x) = md5(x + salt[:32])`，其中 salt = `WinningHealthTechnologyGroupCo.,`（即完整盐串「WinningHealthTechnologyGroupCo.,Ltd.」取前 32 位）；
- 像素接口签名：POST body 末尾 `&sign=md5(sessionID + imageUID + salt[:32])`。

**误判教训**：「试了几个签名变体都返回空」不能推出「不是签名问题」。用一对已知的（密文，签名）做本地比对，或直接在页面里问 `getMD5Field` 的输出，比盲试参数可靠。

## 4 批量下载

1. GET 查看器 URL 拿 cookie，再 POST `studyInfoServlet.do` 换 sessionID；
2. 按目标 SeriesNumber 过滤，对每层 POST 像素接口；
3. 6 并发；按文件大小做断点续传（字节数等于 Rows×Columns×2 才算完整落盘）；429/5xx 退避重试，最多 4 次；
4. 同时保存 `dicomImageInfo` 响应头为 JSON 元数据；
5. 下载前用 3 张代表层（首/中/尾）验证像素分布：肺窗 CT 应有约 −1000 HU 空气峰、骨/对比剂到 +1200 HU，否则说明拿到的是渲染缓存而非原始阵列。

## 5 本地封装 Part-10 DICOM（pydicom）

FileMetaDataset 用 ExplicitVRLittleEndian、CTImageStorage；preamble 128 个 0 字节；PixelData 原样写入平台返回的字节，不做任何窗宽窗位处理。必写标签：

- 身份：StudyInstanceUID / SeriesInstanceUID / SOPInstanceUID / InstanceNumber（全部来自平台）。其中 SOPInstanceUID 默认取平台 ImageUID，**写入前先校验其是否为合法 DICOM UID**（仅数字与点、总长 ≤64、不以 0 开头分量）；不合法时以 `2.25.<uuid5(ImageUID)>` 派生，并在 manifest 中记录 ImageUID↔SOPInstanceUID 映射；
- 几何：ImagePositionPatient / ImageOrientationPatient / PixelSpacing，逐层保留；
- 数值：RescaleIntercept（−1024）、RescaleSlope（1）、BitsAllocated=16、BitsStored=12、HighBit=11、PixelRepresentation=0、SamplesPerPixel=1、PhotometricInterpretation=MONOCHROME2；
- 窗口：WindowCenter / WindowWidth 取第一层斜杠前的值。

两个必查的坑：

- FrameOfReferenceUID 整序列共用一个（按 SeriesUID 用 uuid5 派生 `2.25.<uuid>`），逐层各生成一个会导致全序列被 PACS/Slicer 当成多个坐标系，MPR 直接散架；
- SliceThickness 与 SpacingBetweenSlices 按实测层间距写（示例 0.8 mm），不要照抄平台字段里的名义值（示例 1 mm），否则体积/重建系统性偏差约 25%。

## 6 验证清单（交付前必跑）

1. 每个文件：偏移 128 是 `DICM` 魔数、pydicom 能解析、像素长度 = Rows×Cols×2；
2. UID 一致性：Study/Series 与清单一致，SOP 全唯一，InstanceNumber 与 studyInfo 清单逐一对应、无缺无重（缺号必须在报告中列明，不得自行补号冒充完整）；
3. 几何校验：相邻层 ImagePositionPatient 差值中位数稳定，无离群层（示例：层间距 0.8 mm，Z 跨度 353.6 = 442×0.8）；
4. 像素完整性：封装后 PixelData 与平台 `.raw` 逐字节比对（示例病例 443/443 全等）；
5. SHA-256 全文件记录，用于去重与追溯。

## 7 交付目录

```text
下载结果/
├── Series_302_肺窗/   00001.dcm … 00443.dcm   （示例命名）
├── Study_信息/
│   ├── instance_manifest.csv   每层 SOP / SliceLocation / 文件大小
│   ├── series_manifest.csv
│   ├── sha256.csv
│   └── verification_report.json
└── 预览/              首/中/尾三张肺窗 PNG 供肉眼核对
```

最终状态只能写：**完整 / 部分完成 / 未完成**。标注「像素原生、头本地重建」，不把它当厂商原始文件对外。
