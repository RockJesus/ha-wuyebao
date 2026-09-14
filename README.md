# 物业宝（深圳家和云联 JHCloud）Home Assistant 集成

把「物业宝（业主）」App 的**远程开门**功能接进 Home Assistant（HAOS / Core 均可）。
装好后，每个门禁会变成一个 HA 按钮，可手动点、可语音、可写自动化（回家自动开门等）。

> ✅ **本版为「深圳家和云联网络有限公司」物业宝的专用版**：直接用**手机号 + 密码**登录官方服务器
> `https://wuye.jhws.top/`，**不需要抓包**。接口契约已通过对官方 APK
> （WuYeBao-2025-09-15-1.1.1.51）的逆向与线上验证确认，详见《[接口说明.md](接口说明.md)》。

---

## 功能

| 实体 | 类型 | 说明 |
| --- | --- | --- |
| 每个门禁/大门 | 按钮（Button） | 点击即远程开门，支持 `button.press` 服务与自动化 |
| 每个门禁/大门 | 传感器（Sensor） | 门禁状态及小区/楼栋/单元/对讲号码等属性 |
| 业主 | 传感器（Sensor） | 当前登录业主的账号信息 |
| 最近开门结果 | 传感器（Sensor） | 最近一次开门成功/失败及时间 |
| 门禁数量 | 传感器（Sensor） | 当前发现的门禁数量 |

支持功能：

- 手机号 + 密码登录（官方登录接口 `POST /api/client/anon/token`，自动获取 token）
- token 过期自动刷新（`/api/client/anon/refresh_token`），刷新失败自动重登
- 门禁列表自动发现（`GET /api/device/grant/gates`）
- 开门服务：`wuyebao.open_gate`（可用在自动化/脚本里）
- 开门接口路径、请求方式可在「选项」中配置（默认 `POST api/device/grant/gates/{gateId}/unlock`）
- 多账号：可添加多个配置条目
- 登录失效自动进入重新认证（Reauth）流程

---

## 目录结构

```
wuyebao-integration/
├── custom_components/
│   └── wuyebao/            # ← 把这个文件夹放到 HA 的 custom_components 下
│       ├── __init__.py     # 集成入口 + open_gate 服务
│       ├── manifest.json   # 元数据
│       ├── const.py        # 常量与已验证的接口路径
│       ├── api_utils.py    # 纯函数解析层（可独立测试）
│       ├── api.py          # HTTP 客户端（登录/刷新/门禁/开门）
│       ├── coordinator.py  # 数据轮询与 token 管理
│       ├── config_flow.py  # 配置向导（手机号+密码）
│       ├── button.py       # 开门按钮
│       ├── sensor.py       # 门禁/业主/状态传感器
│       ├── strings.json    # 界面文案
│       └── translations/   # 中英文翻译
├── tests/                  # 单元测试（30 个用例）
├── hacs.json               # HACS 元数据
└── 接口说明.md              # 已验证的 API 契约（替代抓包）
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
2. 将解压后的 `custom_components/wuyebao` 上传到 `/config/custom_components/`。

**方式三：HACS**
1. 安装 HACS 后：HACS → 三个点 → 自定义存储库 → 填本项目的 GitHub 地址，类别选「集成」。
2. 下载后重启 HA。

> 安装后必须**重启 Home Assistant**。

---

## 使用步骤

### 添加集成（不需要抓包）

1. 设置 → 设备与服务 → 添加集成 → 搜索「物业宝（家和云联）」。
2. 填入物业宝（业主）App 的**手机号**和**密码**，提交。
3. 集成会自动查询该手机号绑定的小区：
   - 只绑定一个小区 → 自动选中；
   - 绑定多个小区 → 下拉选择要接入的小区。
4. 登录并选中小区后，即自动创建门禁按钮和传感器实体。

> 服务器地址、`client_id`、开门接口路径、小区参数名等已内置默认值（来自官方 App），
> 一般无需改动；如需调整，进入集成「选项」即可。
>
> 如果是升级自旧版本：请先删除旧配置，再重新添加，以完成小区选择。

### 开门服务

```yaml
# 自动化 / 脚本中开门
action:
  - service: wuyebao.open_gate
    data:
      gate_id: "门禁ID（可在门禁按钮实体的唯一ID或诊断属性里看到）"
```

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
| 添加时提示「无法连接」 | HA 无法访问 `wuye.jhws.top`，检查网络；或在「选项」里换用 `https://wuye.jhws.top/` |
| 提示「登录失败」 | 手机号或密码错误（官方返回 `帐号或密码错误！`）；确认和物业宝 App 里能登录的账号一致 |
| 提示「未查询到绑定的小区」 | 该手机号在物业宝中没有开通业主/小区绑定，需先在物业处开通 |
| 集成已添加但没有按钮 | 门禁接口返回的结构未识别，或该账号没有绑定门禁；查看 HA 日志中的 warning |
| 日志报「必须选择一个小区才能查询信息！」 | 旧版本配置缺少小区信息：删除该集成重新添加（新版会自动选择小区） |
| 按钮点了但门没开 | 见下方「关于开门」说明；可在「选项」里调整开门接口路径/方式 |
| 每隔一段时间失效 | token 过期属正常现象，本集成会自动刷新或重登 |

---

## 关于「开门」的重要说明

对官方 App 的逆向结果表明：物业宝是**云对讲（SIP）**产品，App 内的「手机开门」通过
**SIP 呼叫门禁设备**（对讲自动应答即开门）实现，App 二进制中**不存在**公开的 HTTP
「开门」路径常量（仅存在登录、门禁列表等 HTTP 接口）。

因此本集成内置的开门调用使用**最可能的 REST 风格路径**作为默认值：

```
POST api/device/grant/gates/{gateId}/unlock
```

并支持在「选项」中修改**路径**与**请求方式**（POST / GET），`{gateId}` 会自动替换为门禁 ID。
**该默认路径已在用户环境中验证为 404**，说明该小区开门大概率必须走 SIP 云对讲。

诊断方式（v2.4.0）：① 集成启动/登录时会打一条 `物业宝登录响应(完整字段): {...}`；
② 首次轮询会打一条 `业主响应(第一条完整): {...}`；③ 点开门按钮失败时会打
`开门诊断结果: [...]`（9 个 HTTP 候选）。把这三类日志发给维护者即可确定 SIP 凭据来源
（登录响应或业主响应中可能含 `sipToken` / `sipServer` / `sipAccount` 等字段）与正确开门方式。

- 若你的物业服务器支持 HTTP 开门接口（路径在「选项」里改）→ 按钮直接可用。
- 若服务器不接受（已见 404）→ 需要 SIP 方案：在 HA 侧用 SIP 客户端拨打门禁的
  `对讲号码`（callNumber），或联系物业确认是否有业主端 HTTP 远程开门能力。

---

## 安全说明

- 手机号和密码以 Home Assistant 的配置条目方式**加密存储在本机**，不会上传到任何第三方。
- 请勿把 HA 直接暴露到公网；远程访问请走官方 Cloud、VPN 或反向代理并开启鉴权。
- 「开门」是安全相关动作，自动化里建议加**时间、人在家、门磁确认**等条件，避免误触发。

---

## 开发与验证

- 单元测试（不依赖 HA 运行环境，34 个用例）：
  ```bash
  python -m unittest discover -s tests -v
  ```
- 兼容性：Home Assistant 2024.2+（HAOS 任意较新版本均可）。

## 后续可扩展

- 缴费账单、报修、公告、访客邀请 → 对应接口已确认，可扩展为传感器/服务
- SIP 云对讲开门 → 在 HA 侧集成 SIP 客户端后，可直接拨打门禁对讲号码
- 电梯召梯（云对讲系）→ 同类 SIP 呼叫能力
