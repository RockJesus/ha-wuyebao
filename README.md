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

## ⚠️ 开门方式说明（v2.6.1）

**物业宝业主端没有 HTTP 开门接口**（官方 APK 逆向 + 线上实测：默认路径与全部候选
方案均返回 404），开门实际走 **SIP 云对讲**。逆向官方 APK（libpjsua2.so）确认：
App 的 SIP 栈为 **PJSIP/pjsua2**，传输为 **TCP**（配置串 `sip:sip.jhws.top;transport=tcp`），
SIP 服务器集群为 `sip.jhws.top:5060` / `sip.jhws.top:58583` / `new-sip.jhws.top:58583`
（OpenSIPS 2.1.2，Digest/MD5 鉴权，realm `new-sip.jhws.top` 或 `sip.jhws.top`）。
服务器对**无有效凭据**的 REGISTER/INVITE 一律静默丢弃（只对 OPTIONS 回 407），
所以只有用**真实 accessToken** 才能测出 200/403。

因此 v2.6.2 在点击开门时：
1. 先按配置的 HTTP 路径尝试开门（兼容有 HTTP 开门能力的物业部署）；
2. 失败后自动执行 **SIP 诊断（TCP，3 端点并行）**：先用 OPTIONS 从每个端点
   获取 Digest challenge（realm + nonce），再用 **预置鉴权** 的 REGISTER
   （（手机号 / userId）× accessToken）与 **预置鉴权** 的 INVITE（门禁记录里的
   全部标识符：uid / gateId / deviceNumber / buildingId / unitId / areaId /
   communityCode / bindingCode / 区域+设备号组合）逐一发送，所有结果打日志
   （**Token 一律脱敏**）。

> **为什么必须预置鉴权**：实测 JHCloud 的 OpenSIPS 代理对**不带鉴权的
> REGISTER / INVITE 直接静默丢弃**（只有 OPTIONS 回 407），所以必须先拿
> nonce、把 Digest 算好放进首包发出——否则 REGISTER/INVITE 永远是 status 0，
> 即使凭据正确也测不出来。

诊断日志形如（HA 日志中搜索 `SIP 开门诊断结果`）：

```
INFO ... SIP 开门诊断结果: [
  {"server": "sip.jhws.top:5060", "options": 407},
  {"server": "sip.jhws.top:5060", "step": "register:phone+access", "status": 0, ...},
  ...
  {"server": "new-sip.jhws.top:58583", "step": "invite:unitId", "status": 0, ...}
]
```

- `options: 407` = 服务器可达（TCP 通，鉴权要求）；`options: 0` = 该端点从你的
  网络不可达；
- `status: 0` = 服务器无响应（该凭据/目标不成立）；
- `status: 200 / 180 / 183`（INVITE）= 呼叫成功，**这就是可以固化为开门方案的组合**；
- 启动时会把**全部门禁原始记录**打印到日志（搜索 `门禁列表共 N 条`），
  用于确认每个门禁的 `callNumber` / `sip` 相关字段。

**如果你想让开门真正一键可用**，请把 `SIP 开门诊断结果` 和 `门禁列表共 N 条`
两类日志发给维护者——有了「哪个服务器 + 哪组凭据 REGISTER 成功 + 哪个目标
INVITE 成功」，就能把诊断固化为一键开门。

> 若你的物业提供了 HTTP 开门接口（部分项目部署了），在「选项」里填上接口路径即可，
> 无需 SIP。

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
│       ├── sip.py          # 极简 SIP 客户端（云对讲开门诊断）
│       ├── sensor.py       # 门禁/业主/状态传感器
│       ├── strings.json    # 界面文案
│       └── translations/   # 中英文翻译
├── tests/                  # 单元测试（43 个用例）
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
