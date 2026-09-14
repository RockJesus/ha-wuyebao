# 物业宝（WuyeBao）Home Assistant 集成

把小区物业 App / 小程序「物业宝」的**远程开门**功能接进 Home Assistant（HAOS / Core 均可）。
装好后，每个门禁会变成一个 HA 按钮，可手动点、可语音、可写自动化（回家自动开门等）。

> ⚠️ **重要前提**：市面上叫「物业宝」的产品由多家公司运营，**都没有公开的开放 API**。
> 本集成采用「抓包适配」的方式：先抓你自己手机上 App 的请求，把真实接口地址和字段填进配置即可。
> 抓包教程见《[抓包指南.md](抓包指南.md)》。

---

## 功能

| 实体 | 类型 | 说明 |
| --- | --- | --- |
| 每个门禁/大门 | 按钮（Button） | 点击即远程开门，支持 `button.press` 服务与自动化 |
| 最近开门结果 | 传感器（Sensor） | 最近一次开门成功/失败及时间 |
| 门禁设备数 | 传感器（Sensor） | 当前发现的设备数量 |

支持功能：

- 手机号 + 密码登录（自动获取 token、过期自动重登）
- 设备列表自动发现（可配置设备 ID / 名称字段名）
- 登录、设备列表、开门三个接口路径均可配置（POST / GET 可选）
- 多小区多账号：可添加多个配置条目
- 登录失效自动进入重新认证（Reauth）流程

---

## 目录结构

```
wuyebao-integration/
├── custom_components/
│   └── wuyebao/            # ← 把这个文件夹放到 HA 的 custom_components 下
│       ├── __init__.py     # 集成入口
│       ├── manifest.json   # 元数据
│       ├── const.py        # 常量与默认值
│       ├── api_utils.py    # 纯函数解析层（可独立测试）
│       ├── api.py          # HTTP 客户端（登录/设备列表/开门）
│       ├── coordinator.py  # 数据轮询与 token 管理
│       ├── config_flow.py  # 配置向导
│       ├── button.py       # 开门按钮
│       ├── sensor.py       # 状态传感器
│       ├── strings.json    # 界面文案
│       └── translations/   # 中英文翻译
├── tests/                  # 单元测试
├── hacs.json               # HACS 元数据
└── 抓包指南.md             # 抓包教程
```

---

## 安装（HAOS）

任选一种方式把 `custom_components/wuyebao` 复制到 HA 的 `/config/custom_components/`：

**方式一：Samba（推荐，图形界面）**
1. 设置 → 加载项商店 → 搜索安装 **Samba Share**，启动并开启「启动时自动运行」。
2. 电脑上打开 `\\<HA的IP>\config\custom_components\`。
3. 把本项目的 `custom_components/wuyebao` 整个文件夹复制进去。

**方式二：SSH 终端**
1. 安装 **Advanced SSH & Web Terminal** 加载项。
2. 在终端执行：
   ```bash
   # 把下载好的压缩包解压后，将 custom_components/wuyebao 上传到 /config/custom_components/
   ls /config/custom_components/wuyebao/
   ```

**方式三：HACS**
1. 安装 HACS 后：HACS → 三个点 → 自定义存储库 → 填本项目的 GitHub 地址，类别选「集成」。
2. 下载后重启 HA。

> 安装后必须**重启 Home Assistant**。

---

## 使用步骤（两步）

### 第 1 步：抓包拿到真实接口

打开手机 App / 小程序，分别操作：**登录 → 打开门禁列表页 → 点一次开门**。
用抓包工具记录下这三个请求，你需要的信息：

| 需要拿到的信息 | 对应配置项 |
| --- | --- |
| 服务器地址（例如 `https://api.xxx.com`） | API 服务器地址 |
| 登录请求路径（例如 `/api/login`） | 登录接口路径 |
| 设备列表请求路径 | 设备列表接口路径 |
| 开门请求路径 + 参数名（例如 `deviceId`） | 开门接口路径 / 设备ID字段名 |

详细图文步骤见 **《[抓包指南.md](抓包指南.md)》**。

### 第 2 步：添加集成

1. 设置 → 设备与服务 → 添加集成 → 搜索「物业宝」。
2. 填手机号、密码、API 服务器地址 → 提交。
3. 若登录成功，会自动创建按钮和传感器实体；如果设备列表里的字段名不同，进入「选项」调整。
4. 在「选项」里可以修改三个接口路径、请求方式、字段名、轮询间隔。

---

## 使用与自动化示例

手动开门：设备与服务 → 物业宝 → 点击对应按钮。

自动化（回家开门，建议配合门磁/人体存在做防呆）：

```yaml
alias: 回家自动开门
trigger:
  - platform: zone
    entity_id: person.zhangsan
    zone: zone.home
    event: enter
condition:
  - condition: time
    after: "06:00:00"
    before: "23:00:00"
action:
  - service: button.press
    target:
      entity_id: button.xiaoqu_beimen
```

语音控制（需要小爱/天猫精灵等桥接，或 HA 的 Assist）：
```
“打开小区北门”
```

---

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 添加时提示「无法连接」 | 服务器地址不对或 HA 无法访问该地址；检查是否漏了 `https://`、端口是否正确 |
| 提示「登录失败」 | 账号密码错误，或该平台的登录接口字段不是 `phone/password`——把抓到的登录请求体发我，帮你调整 |
| 集成已添加但**没有按钮** | 设备列表接口返回的结构没被识别。在「选项」里改「设备列表接口路径」，或把抓到的列表 JSON 发我 |
| 按钮点了但门没开 | 开门接口参数名不同（如 `doorId`/`id`），在「选项」里改「设备ID字段名」和「开门接口路径」 |
| 每隔一段时间失效 | token 过期属正常现象，本集成会自动重登；若仍失效说明接口需要额外鉴权参数，把抓包结果发我 |
| 想开多个小区的门 | 再次添加集成，用另一个账号即可 |

> **把抓包结果发给我**，我可以把你的接口信息直接写死成专用适配版，以后无需再配置。

---

## 安全说明

- 手机号和密码以 Home Assistant 的配置条目方式**加密存储在本机**，不会上传到任何第三方。
- 请勿把 HA 直接暴露到公网；远程访问请走官方 Cloud、VPN 或反向代理并开启鉴权。
- 「开门」是安全相关动作，自动化里建议加**时间、人在家、门磁确认**等条件，避免误触发。
- 抓包得到的 token / 密码等同账号权限，不要截图发到公开平台。

---

## 开发与验证

- 单元测试（不依赖 HA 运行环境）：
  ```bash
  python -m unittest discover -s tests -v
  ```
- 兼容性：Home Assistant 2024.2+（HAOS 任意较新版本均可）。

## 后续可扩展

- 缴费账单、报修、公告 → 增加传感器实体与对应接口
- 蓝牙开门 → 需要额外的手机端中转方案
- 若你确认了具体厂商（如深圳聚合云、绿城、狄耐克等），可针对性写死专用适配器
