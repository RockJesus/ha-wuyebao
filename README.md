# 物业宝 Home Assistant 集成（wuyebao）

[![HACS Default](https://img.shields.io/badge/HACS-Default-orange.svg)](https://hacs.xyz/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Home Assistant 自定义集成（集成域名 `wuyebao`），用于连接物业宝 App，实现门禁控制等功能。

> 仓库：https://github.com/RockJesus/ha-wuyebao

## 功能特性

- ✅ 用户名密码登录（client_id 与 App 对齐）
- ✅ Token 自动刷新（长期有效，突破7天限制）
- ✅ 门禁设备列表自动发现（**返回小区全部门**：南门、北门 + 各楼栋单元门口机，按楼栋/单元正确命名；实体按稳定 gate id 注册，更新不会删除已有门）
- ✅ 门锁实体（每个门禁一个实体，共 26 个门）
- ✅ SIP 开门（南门、北门、单元门均正常）
- ✅ 监控摄像头实体（每个门口机一个，显示最近呼叫抓拍图）
- ✅ **实时视频流**（内置 RTSP 服务器，支持 go2rtc / ffmpeg 拉流观看监控）
- ✅ 流地址传感器（每个门禁设备一个，直接显示 RTSP 地址供 go2rtc 复制）
- ✅ 传感器：用户名、小区信息
- ❌ 电梯呼叫（暂不支持）

## 安装

### 方法一：HACS 安装（推荐）

1. 确保已安装 [HACS](https://hacs.xyz/)
2. 在 HACS 中搜索 "物业宝" 或 "wuyebao"
3. 点击下载并重启 Home Assistant

### 方法二：手动安装

1. 下载 `custom_components/wuyebao/` 文件夹
2. 将其复制到 Home Assistant 的 `custom_components/` 目录
3. 重启 Home Assistant

## 配置

1. 在 Home Assistant 中，进入 **设置** > **设备与服务**
2. 点击 **添加集成**
3. 搜索 "物业宝"
4. 输入手机号和密码
5. 点击提交

## 实体说明

### 门锁实体

每个门禁设备会自动创建一个门锁实体：

- **名称**：根据设备别名/楼栋单元自动命名（如"南门"、"4栋1单元"）
- **状态**：始终显示锁定，解锁后 10 秒自动恢复锁定
- **解锁**：调用 `lock.unlock` 服务发送开门指令

**属性**：
- `device_id` - 设备 ID
- `device_type` - 设备类型（outdoor/wall）
- `unlock_password` - 开门密码
- `sip_target` - SIP 目标地址

### 传感器实体

- **用户名** - 当前登录账号
- **小区** - 所在小区名称

### 摄像头实体

每个带摄像头的门禁设备（围墙门、门口机）会自动创建两个摄像头实体：

**监控摄像头**
- **名称**：根据设备别名/楼栋单元自动命名（如"南门监控"、"4栋1单元门口机1监控"）
- **图像**：显示该门禁设备最近的呼叫/报警抓拍图（严格按设备号匹配，不会串图）
- **挂载**：与门锁实体挂载在同一设备下

**实时监控摄像头（实时视频流）**
- **名称**：如"南门实时监控"、"4栋1单元门口机1实时监控"
- **机制**：集成内置 RTSP 服务器，通过 SIP INVITE 建立监控会话，H264 RTP 流经内置 RTSP 服务器转发（`rtsp://127.0.0.1:8555/<gate-id>`）
- **go2rtc 集成**：在 go2rtc 配置中添加 `rtsp://127.0.0.1:8555/<gate-id>` 源即可转 WebRTC 实时观看
- **自动重试**：无媒体时自动挂断重试（最多 3 次），设备忙时自动等待重拨
- **NAT 打洞**：自动复制 App 的 "probing data" + RTCP 打洞机制打通媒体通道

**工作原理**：
1. 登录后获取业主信息（含室内机 `bindingCode`）
2. 调用呼叫记录接口获取该室内机收到的门口机呼叫记录
3. 按设备号严格匹配呼叫记录中的 `imageUrl`（阿里云 OSS 抓拍图）
4. 摄像头实体展示匹配的最新抓拍图
5. 实时视频流：RTSP 客户端拉流时，集成通过 SIP REGISTER → INVITE 建立与门口机的监控会话，收到 H264 RTP 后转发给 RTSP 客户端

**说明**：
- 单元门口机（outdoor）有呼叫记录，监控/实时监控均可正常显示
- 南门/北门（wall）如无呼叫/报警记录，摄像头显示"不可用"（不会错误显示其他门的图像）
- 可在物业宝 App 中按铃呼叫单元门口机，生成新的抓拍记录

**属性**：
- `device_id` - 设备 ID
- `device_type` - 设备类型（outdoor/wall）

### 按钮实体

每个带摄像头的门禁设备也会创建一个"查看监控"按钮：

- **功能**：点击后通过 SIP 发送监控指令，触发门口机实时抓拍
- **配合摄像头实体**：点击按钮后刷新摄像头图像可获取最新抓拍

## API 说明

本集成基于物业宝 App 抓包分析，主要接口：

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/client/anon/token` | POST | 登录获取 Token（请求头需带 App 的 client_id） |
| `/api/client/anon/refresh_token` | GET | 刷新 Token |
| `/api/client/anon/client_token` | GET | 获取 SIP Token |
| `/api/device/grant/gates` | GET | 获取门禁列表（按 communityId+unitId+type=indoor 过滤，只返回本单元设备） |
| `/api/owner/anon/owners/community` | GET | 获取业主信息 |
| `/api/owner/grant/owners` | GET | 获取业主详情（含室内机 bindingCode、unitId） |
| `/api/call/{page}/{size}/grant/calls` | GET | 获取呼叫记录（含门口机抓拍图） |

## 更新日志

### v6.6.1
- 集成域名由 `propertybao` 更改为 **`wuyebao`**（项目更名，仓库地址：https://github.com/RockJesus/ha-wuyebao）
- 隐私审计：代码内无硬编码手机号、密码、用户 token 等个人信息；`client_id`/SIP 凭据为 App 公共标识（非个人数据）
- 所有门设备保留逻辑不变（实体按稳定 gate id 注册，更新不会删除门）

### v6.6.0
- 添加回小区**所有门**：门禁列表不再按单元过滤，返回全小区全部设备（南门、北门 + 各楼栋单元门口机，共 26 个）
- 每个门按 楼栋/单元 正确命名（如"长乐湾小区 1区 4栋 1单元 门禁-1"），实体按稳定 gate id 注册，**后续更新不会删除已有门**

### v6.5.0
- 修复 RTSP 端口冲突：内置 RTSP 服务器端口由 8555 改为 **8556**（8555 与 go2rtc 的 WebRTC 端口冲突导致实时视频无法拉流），实时视频恢复正常
- 流地址传感器同步更新为 `rtsp://127.0.0.1:8556/<gate-id>`

### v6.4.1
- 修复单元门识别：`/api/owner/grant/owners` 返回全小区业主列表，旧代码取第一条导致单元门识别为其他楼栋；现按登录手机号精确匹配当前业主，正确识别"4栋1单元"门口机（南门、北门、门口机均正确）

### v6.4.0
- 修复登录 client_id：与 App 实际值对齐（`984136508489469952`），登录更稳定
- 修复门禁列表参数：按 `unitId + type=indoor` 过滤，只显示本小区/本单元的门禁（南门、北门、门口机），不再拉取全小区 26 个门口机，设备识别更准确

**开门机制**：开门通过 SIP MESSAGE 发送，目标地址为设备 SIP 账号，消息体为 JSON 格式的解锁指令。

## 注意事项

- 本集成仅供学习交流使用
- 请遵守物业宝相关服务条款
- 开门功能已支持南门、北门、单元门
- 摄像头实体显示门口机最近呼叫抓拍图，可通过"查看监控"按钮触发实时抓拍
- 如有问题请提交 Issue

## 开发

### 环境要求

- Home Assistant 2023.1+
- Python 3.10+

### 目录结构

```
wuyebao-ha/
├── custom_components/
│   └── wuyebao/
│       ├── __init__.py      # 集成入口（含 RTSP 服务器生命周期）
│       ├── const.py         # 常量定义
│       ├── api.py           # API 客户端
│       ├── config_flow.py   # 配置流程
│       ├── manifest.json    # 集成清单
│       ├── lock.py          # 门锁平台
│       ├── sensor.py        # 传感器平台
│       ├── camera.py        # 摄像头平台（实时视频流）
│       ├── sip.py           # SIP 客户端（开门）
│       ├── sip_video.py     # SIP 监控呼叫（INVITE/RTP/打洞）
│       ├── rtsp_server.py   # 内置 RTSP 服务器
│       └── translations/    # 翻译文件
├── .github/                 # GitHub 配置
├── README.md                # 说明文档
├── LICENSE                  # 开源协议
└── hacs.json                # HACS 配置
```

## License

MIT License
