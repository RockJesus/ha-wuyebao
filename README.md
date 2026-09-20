# 物业宝 Home Assistant 集成

[![HACS Default](https://img.shields.io/badge/HACS-Default-orange.svg)](https://hacs.xyz/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Home Assistant 自定义集成，用于连接物业宝 App，实现门禁控制等功能。

## 功能特性

- ✅ 用户名密码登录
- ✅ Token 自动刷新（长期有效，突破7天限制）
- ✅ 门禁设备列表自动发现
- ✅ 门锁实体（每个门禁一个实体）
- ✅ SIP 开门（南门、北门、单元门均正常）
- ✅ 监控按钮（触发监控呼叫）
- ✅ 传感器：用户名、小区信息
- 🚧 监控视频流（开发中）
- ❌ 电梯呼叫（暂不支持）

## 安装

### 方法一：HACS 安装（推荐）

1. 确保已安装 [HACS](https://hacs.xyz/)
2. 在 HACS 中搜索 "物业宝" 或 "propertybao"
3. 点击下载并重启 Home Assistant

### 方法二：手动安装

1. 下载 `custom_components/propertybao/` 文件夹
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

### 按钮实体

为每个围墙门（南门、北门）自动创建一个监控按钮：

- **名称**：查看监控
- **功能**：点击后发送 SIP 监控呼叫，触发门禁摄像头启动
- **使用**：触发后可在手机 App 上查看实时监控视频

## API 说明

本集成基于物业宝 App 抓包分析，主要接口：

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/client/anon/token` | POST | 登录获取 Token |
| `/api/client/anon/refresh_token` | GET | 刷新 Token |
| `/api/client/anon/client_token` | GET | 获取 SIP Token |
| `/api/device/grant/gates` | GET | 获取门禁列表 |
| `/api/owner/anon/owners/community` | GET | 获取业主信息 |

**开门机制**：开门通过 SIP MESSAGE 发送，目标地址为设备 SIP 账号，消息体为 JSON 格式的解锁指令。

**监控机制**：监控通过 SIP MESSAGE 发送，类型为 `monitor`，触发门禁摄像头启动。

## 注意事项

- 本集成仅供学习交流使用
- 请遵守物业宝相关服务条款
- 开门功能已支持南门、北门、单元门
- 监控按钮仅触发呼叫，视频需在手机 App 查看
- 如有问题请提交 Issue

## 开发

### 环境要求

- Home Assistant 2023.1+
- Python 3.10+

### 目录结构

```
propertybao-ha/
├── custom_components/
│   └── propertybao/
│       ├── __init__.py      # 集成入口
│       ├── const.py         # 常量定义
│       ├── api.py           # API 客户端
│       ├── config_flow.py   # 配置流程
│       ├── manifest.json    # 集成清单
│       ├── lock.py          # 门锁平台
│       ├── button.py        # 按钮平台
│       ├── sensor.py        # 传感器平台
│       ├── sip.py           # SIP 客户端
│       └── translations/    # 翻译文件
├── .github/                 # GitHub 配置
├── README.md                # 说明文档
├── LICENSE                  # 开源协议
└── hacs.json                # HACS 配置
```

## License

MIT License
