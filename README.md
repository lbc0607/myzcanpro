# myzcanpro — UDS 诊断 / ECU 刷新上位机

基于 ZLG(USBCANFD / USBCAN-II)的自研诊断上位机,界面仿 ZCANPro。

## 运行(源码)
需 **32 位** Python(zlgcan.dll 是 32 位)+ PyQt5。ZCANPro 自带的 `python38` 即可:
双击 `run.bat`(路径不同时先设置环境变量 `PY32`)。
不接硬件时,通道选"模拟ECU(测试)"即可体验全部流程。

## 打包成绿色软件
- 本地:双击 `build.bat` → `dist\myzcanpro\myzcanpro.exe`(整个文件夹拷走即可)。
- 云端:推送到 GitHub 后由 `.github/workflows/build.yml` 构建,在 Actions 的 Artifacts 下载。

## 部署目录(与 exe 同级)
- `zlgcan.dll` 与 `kerneldlls\`(从 ZCANPro 目录拷贝)
- 公司密钥 dll 及其依赖;需安装 VC++ 2013 x86(zlgcan)、VC++ 2008 x86(密钥 dll)运行库
- 程序运行后自动生成:`myzcanpro_config.json`、`services.json`、`error.log`、`logs\`

## 方案文件
ECU 刷新方案为 JSON(格式 `myzcanpro-flash`),可自由命名扩展名,各 SID 参数均可编辑。

## 测试
`python tests/run_all.py`(GUI 测试使用桩 Qt,不依赖显示环境)。
