# myzcanpro — UDS 诊断 / ECU 刷新上位机

基于 ZLG(USBCANFD / USBCAN-II)的自研诊断上位机,界面仿 ZCANPro。

## 运行(源码)
需 **32 位** Python(zlgcan.dll 是 32 位)+ PyQt5。ZCANPro 自带的 `python38` 即可:
双击 `run.bat`(路径不同时先设置环境变量 `PY32`)。
不接硬件时,通道选"模拟ECU(测试)"即可体验全部流程。

## 打包成绿色软件
- 本地:双击 `build.bat` → `dist\myzcanpro\myzcanpro.exe`(整个文件夹拷走即可)。
- 云端:推送到 GitHub 后由 `.github/workflows/build.yml` 构建,在 Actions 的 Artifacts 下载。

## 设备管理(主界面)
1. 下拉选择设备(USBCANFD 系列 / USBCAN-II / 模拟ECU),点「打开」→ 按钮变为「关闭」(只能在两种状态间切换)。
2. 打开成功后出现各通道行,点「启动」弹出配置:CAN/CAN FD、波特率(kbaud/s)、正常/只听、终端电阻、是否计算总线负载率(刷新时间默认 200 ms)。
3. 通道启动后,主界面实时显示接收/发送/错误帧数和总线负载率;UDS诊断、ECU刷新窗口里的「通道」下拉只列出已启动的通道。

## 部署目录(与 exe 同级)
- `zlgcan.dll` 与 `kerneldlls\`:仓库 `vendor\` 里已带官方 SDK(zlgcan_x86,2026-10 版),打包时自动复制到 exe 旁;
  源码运行时程序也会自动在 `vendor\` 里查找。官方头文件在 `docs\zlgcan_sdk_headers\`。
- 公司密钥 dll 及其依赖;需安装 VC++ 2013 x86(zlgcan)、VC++ 2008 x86(密钥 dll)运行库
- 程序运行后自动生成:`myzcanpro_config.json`、`services.json`、`error.log`、`logs\`

## 方案文件
ECU 刷新方案为 JSON(格式 `myzcanpro-flash`),可自由命名扩展名,各 SID 参数均可编辑。

## 测试
`python tests/run_all.py`(GUI 测试使用桩 Qt,不依赖显示环境)。
