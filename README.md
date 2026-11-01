# 中国运营商话费监控 (China Carrier Monitor)

一个 Home Assistant 自定义集成，把 **中国电信 / 中国联通 / 中国移动 / 中国广电** 四家运营商的话费与套餐查询合并为一个集成，支持**多账号**（每个号码一个配置条目）与**手机号 + 短信验证码登录**（移动），其余运营商提供验证过的服务密码登录与会话导入方案。

本集成是对以下项目的融合与重写（v1.1 吸收了 Shaobor/Shaobo-Pocket-Carrier 的新一代运营商协议与 summmer121/smsForward 的短信查询思路）：

- [hlhk2017/ChinaTelecomMonitor-Homeassistant-Integration](https://github.com/hlhk2017/ChinaTelecomMonitor-Homeassistant-Integration)（电信）
- [hlhk2017/homeassistant-unicom_bill_info](https://github.com/hlhk2017/homeassistant-unicom_bill_info)（联通）
- [Cyborg2017/ha_unicom_bill](https://github.com/Cyborg2017/ha_unicom_bill)、[Testplayers/unicom-mini-ha](https://github.com/Testplayers/unicom-mini-ha)、[sfairy/ha_china_comm](https://github.com/sfairy/ha_china_comm)（联通/电信参考）
- [cumtwjq/china-mobile-10086-ha](https://github.com/cumtwjq/china-mobile-10086-ha)（移动参考）

## 功能

- **四运营商**：电信（**短信验证码·自动过官方滑块** / 服务密码）、联通（**短信验证码** / 服务密码 / 小程序 openid）、移动（**短信验证码** / 服务密码 / Cookie，查询走 12.5 原生协议+refreshSession 保活）、广电（浏览器会话导入）
- **多账号**：重复添加集成即可，每账号独立会话与设备，unique_id 唯一防重复
- **传感器**：话费余额、本月消费、套餐名称、积分、通用/定向/总流量（剩余+已用）、语音剩余、短信剩余，附用量明细属性
- **会话状态实体**：`session_state`（healthy / degraded / needs_human / circuit_open），永远可读——掉线自动化直接告警它，不用猜哪个数值实体变成了 unavailable
- **自适应轮询**：认证失败后轮询间隔自动按 ×1/×2/×4/×8 拉长（封顶 24 小时），Cookie 失效的账号不再按固定间隔空转刷风控
- **自动重新认证**：会话失效时自动发起 reauth 配置流，通知你重新登录
- **服务**：`cn_carrier_monitor.refresh` 手动刷新（可指定 `entry_id` 或 `phone`）；`cn_carrier_monitor.inject_token` 调试/运维用，把浏览器收割的 token/Cookie 直接灌入指定号码并重载条目
- **集成图标**：自带 `brand/` 品牌图（含暗色变体与 @2x），HA 2026.3+ 自动显示，无需任何配置
- HACS 可装；中文界面

## 安装

> **版本要求：需要 Home Assistant 2025.3 或更高。** 集成使用
> `AddConfigEntryEntitiesCallback`（2025.3 引入）注册实体平台，更早的版本
> 会因 ImportError 导致 sensor/button 平台整体加载失败（表现为一个实体都没有）。

### HACS（推荐）

1. HACS → 右上角菜单 → 自定义存储库
2. 填入本仓库地址，类别选「集成」
3. 安装后**重启 Home Assistant**

### 手动

把 `custom_components/cn_carrier_monitor` 整个目录复制到 HA 配置目录的 `custom_components/` 下，重启。

## 各运营商接入指南

### 中国电信（短信验证码 / 服务密码）

**短信登录（推荐）**：填手机号提交，集成自动完成官方滑块验证并下发短信，输入验证码即登录，无需服务密码。

**服务密码**：

1. 服务密码是电信网上营业厅的查询密码（不是通话密码），忘记可发短信 `MMCZ#身份证后六位` 到 10001 重置，或用电信 App 重置。
2. 集成 → 添加 → 「中国运营商话费监控」→ 中国电信 → 输入手机号 + 服务密码。
3. 若提示「设备未信任」：先用官方 App 正常登录一次该账号，或把可选的「设备信任 ID」填上后重试。

### 中国联通（服务密码 / openid）

- **服务密码（推荐）**：中国联通 App 的服务密码。填手机号 + 服务密码即可，token 失效会自动续期。
- **openid（无密码兜底）**：手机微信打开「中国联通」小程序 → 抓包工具（如 Reqable/Charles）过滤 `mina.10010.com` → 找到任意请求 URL 里的 `openid` 参数复制填入。

### 中国移动（短信验证码 / 服务密码 / Cookie）

- **短信验证码（推荐）**：填手机号 → 提交后该号码收到短信 → 输入验证码完成登录。纯 HTTP 实现（login.10086.cn 链路），无需浏览器。
- **服务密码**：网上营业厅查询密码。
- **Cookie 导入**：浏览器登录 `wx.10086.cn` → DevTools 里复制任意请求的 `Cookie` 头 → 粘贴。
- 会话失效后集成会发起 reauth，按提示重新登录即可。

### 中国广电（会话导入）

广电官网 (`www.10099.com.cn`) 有阿里云 WAF，短信登录无法在服务端复现，采用与 [liuliang-buddy](https://github.com/liuliang-buddy/liuliang-buddy) 相同的「会话导入」方案：

1. 电脑浏览器登录 `www.10099.com.cn`（短信验证码）
2. 进入「个人中心 / 号码余量」页面，F12 打开 Network，找到包含 `qryUserRes` 的请求
3. 右键 → Copy → **Copy as cURL**，整段粘贴到配置框（或分别填请求 URL 和 Cookie 头）
4. 官网会话约 3 天后过期，届时集成会发起 reauth，重复上述步骤

## 实体说明

每账号一台设备，包含：

| 实体 | 说明 | 单位 |
|---|---|---|
| 话费余额 | 可用余额（电信欠费时为负数） | ¥ |
| 本月消费 | 本账期实时话费/账单合计 | ¥ |
| 套餐名称 | 当前套餐 | - |
| 会话状态 | healthy / degraded / needs_human / circuit_open（永远可读） | - |
| 通用流量剩余/已用 | 含用量明细属性（总量、使用率） | GB |
| 定向流量剩余 | GB |
| 总流量剩余 | GB |
| 语音剩余 | 分钟 |
| 短信剩余 | 条 |
| 积分 | 电信/部分联通账号 | 分 |

> 各运营商接口返回能力不同，缺失的数据实体自动置为不可用。

## 自动化示例

低余额提醒：

```yaml
automation:
  - alias: 话费余额不足提醒
    triggers:
      - trigger: numeric_state
        entity_id: sensor.zhongguo_dianxin_173xxxx35869_hua_fei_yu_e
        below: 10
    actions:
      - action: notify.persistent_notification
        data:
          message: "电信卡余额不足10元，请及时充值"
```

流量使用率超过 90%（读取实体属性）：

```yaml
triggers:
  - trigger: template
    value_template: >
      {{ state_attr('sensor.xxx_tong_yong_liu_liang_sheng_yu', '用量明细')
         and (state_attr('sensor.xxx_tong_yong_liu_liang_sheng_yu','用量明细').使用率 or '') | replace('%','') | float(0) > 90 }}
```


## 工程质量（v1.2+）

[![CI](https://github.com/yahooor/cn-carrier-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/yahooor/cn-carrier-monitor/actions/workflows/ci.yml)

- **契约测试**：`tests/` 以真实响应结构 fixtures 固化四运营商解析契约（125 用例，含上线过程中每个 bug 的回归用例）
- **CI 门禁**：ruff（含 F821 未定义名，历史上 `empty_data` 类事故的机器防线）、pytest、hassfest、HACS 校验
- **协议基线**：[docs/协议基线.md](docs/协议基线.md)——每个密钥/端点的来源与本环境实证状态、漂移症状与防御

### 限频预算（重要）

| 运营商 | 默认间隔 | 下限 | 说明 |
|---|---|---|---|
| 电信 | 30 分钟 | ≥5 分钟 | 密码模式 token 长效；短信模式实测 token ≥30 小时（81 次连续成功） |
| 联通 | 10-30 分钟 | ≥5 分钟 | onLine 保活 + 续期回写；**mall Cookie 模式 3-5 小时必失效，需人工重导** |
| 移动 | 30 分钟 | ≥5 分钟 | refreshSession 保活顺延 Cookie；单设备互斥，手机开 App 即顶号 |
| 广电 | 60 分钟 | ≥30 分钟 | 会话约 3 天，重放请求勿高频 |

连续失败 ≥5 次自动熔断 15 分钟（**含认证失败**，保护风控额度），期间实体保留最后一次数据。认证失败期间轮询间隔自动按 ×1/×2/×4/×8 退避（封顶 24 小时）。会话彻底失效时发**一次性**通知提示重新认证，恢复后自动消失。

### 威胁模型与凭据

- 服务密码/token/cookie 存于 HA 配置条目（`.storage`，随 HA 备份）；HA 本机泄露即泄露，请保证 HA 主机访问安全
- webhook `cn_carrier_monitor_sms`：**未配置密钥时只接受内网/回环来源**；跨网（如手机蜂窝网络回传）必须在集成**选项**里设置 `webhook_secret`，请求带同值 `X-Secret` 头或 body `secret` 字段。同一账号 10 分钟内最多提交 5 次验证码（429）。**注意**：HA 前挂反向代理（nginx/frp 等）时 `request.remote` 是代理的 127.0.0.1，"内网来源"判定会被绕过——此类部署**必须**配置 `webhook_secret`
- 出站 TLS **默认校验证书**。若某些运营商网关使用自签/过期证书导致连接失败，可临时设置环境变量 `CN_CARRIER_INSECURE_TLS=1` 关闭校验（会失去 MITM 防护，日志持续告警）
- 本集成仅查询个人账号数据；禁止高频轮询（见预算表）与商业用途

## 更新日志

### v1.10.1（代理出口切换自愈重试）

- 配置了「出站代理」的移动账号，若本轮 5 个网厅端点全部传输失败（≥4 且无漂移证据），判定为代理节点切换（`d.sid` 与新出口 IP 不匹配），**当轮立即重跑一次 newHome 自愈重签 `d.sid` 并重试 4 个数据端点**——把节点漂移的不可用窗口从 30-60 分钟压缩到本轮内。未配置代理时不重试（直连出口稳定，避免风控暴露）
- 测试 148 → 150（+2：重试触发/直连不重试），变异验证 RED

### v1.10.0（出站代理支持——移动掉线根治）

- **集成选项新增「出站代理 URL」**：移动网厅 `d.sid` 与签发出口 IP 绑定（已实证：跨出口使用必 500）。若移动账号反复掉线，在集成「选项」里填一个固定 HTTP 代理（如 `http://192.168.31.123:7890`），让凭据的签发与使用共用同一出口。留空 = 直连（默认行为不变）
- 仅影响本集成的运营商出站请求（aiohttp + curl_cffi 双链路都走代理）；绑定后须在代理端保证该出口长期在线
- 测试 145 → 148（+3），变异 2/2 RED

### v1.9.0（移动 12.5 App 原生通道接线）

- **移动新增「App 抓包导入」登录方式**：抓包工具捕获「中国移动」App 的 `getFamilyCube`/`getRealFee` 请求，复制 cURL（或含 JSESSIONID 的 Cookie）粘贴即可。原生通道（clientaccess.10086.cn）凭据保活数周，彻底绕开网厅 `d.sid` 30 分钟 TTL 与「IP+会话绑定」风控（已实证：跨出口使用 d.sid 必 500）。绑定后请避免在手机上打开移动 App（单设备互斥会顶号）
- 接线 `use_native` 死配置：配置流写入后 `async_fetch` 优先走原生通道，原生失败自动落回网厅
- 测试 140 → 145（+5：路由注册/缺 Cookie/缺 JSESSIONID/use_native 落库/源码守卫），变异 2/2 RED

### v1.7.1（修复集成图标不显示）

- **图标/logo 从未显示**：改用 HA 2026.3+ 官方的集成内 `brand/` 目录机制——品牌图（含暗色变体与 @2x）随集成分发，前端与 HACS 自动显示。此前依赖 brands CDN（该域从未收录，恒 404）+ 前端 JS 覆盖（HA 前端为 Shadow DOM，`querySelectorAll` 穿不透，方案自始无效）。
- 移除上述死代码（`js/icon_override.js` 与静态路径注册），manifest 移除为图标 hack 引入的 `http` 依赖与 `frontend` after_dependencies。

### v1.7.0（会话状态实体 + 自适应轮询）

- **`session_state` 实体**：healthy / degraded / needs_human / circuit_open 四态，永远可读（掉线时正是最需要可读的时刻），自动化告警不再依赖猜测哪个实体 unavailable。
- **认证失败自适应轮询**：间隔按 ×1/×2/×4/×8 拉长封顶 24 小时，解决 Cookie 失效账号按固定间隔无限空转的问题（线上实测联通 mall Cookie 失效后 22 小时空转 48 次）。
- **移动端点判类三分离**：网络失败 > 结构漂移（404/410 归 `protocol_drift`）> 会话失效，不再把网络故障误报成"掉线请重新抓 Cookie"。
- `refresh` 服务支持按 `phone` 过滤；新增 `inject_token` 运维服务；诊断包新增 `recent_events`（近 50 条事件环形缓冲）。

### v1.6.0（实体集合稳定化 + 误判修正）

> ⚠️ **最低 HA 版本 2024.6 → 2025.3**（`AddConfigEntryEntitiesCallback` 自 2025.3 起提供；旧版会因 ImportError 导致 sensor/button 平台整体加载失败）。

- **实体不再消失**：实体集合与"这次有没有拿到数据"解耦——数据缺失、单端点漂移、首次刷新失败时实体保留（显示 unavailable），不再从状态机消失。首次安装仍是纯数据驱动，不会凭空造出运营商不提供的字段。
- **首次刷新失败改为降级加载**：条目已有实体时不再整条卸载（原 `setup_error` 会让 17 个实体一起消失、诊断也看不到 coordinator）；**只有凭据被拒才弹"重新认证"**，网络抖动等下一轮轮询自愈。
- **认证失败计入熔断**：联通 mall Cookie 失效后不再每 30 分钟空转（线上实测 22 小时 48 次无效重试），连续 5 次即熔断 15 分钟。
- **移动不再把网络故障当成掉线**：单端点解析崩溃隔离为 `protocol_drift` 并记入 `extra.endpoint_anomalies`；端点全部请求失败/HTTP 非 200 归为 `network`，不再引导用户去重新抓 Cookie。判类优先级：网络失败 > 结构漂移 > 会话失效。
- **掉线一次性通知**：进入"需要人工"状态时发一条 `persistent_notification`（含该账号的处理方式），恢复后自动消失，不再刷屏。
- **补齐 4 个缺失翻译键**：`voice_used` / `sms_used` / `flow_directed_used` / `flow_total_used`——此前这些实体 fallback 成设备名，导致同一账号下多个实体同名。
- **修复平台转发失败时的会话泄漏**：`async_forward_entry_setups` 抛异常时回收 aiohttp 会话并从 `hass.data` 清理，避免每次 setup 重试漏一个连接池。
- 新增 37 条回归用例（含变异验证：改回旧行为必须让用例变红），总计 114 passed。

### v1.5.0（稳定性与安全整修）

- **修复**：重启后传感器不再消失（移除了按实体注册表跳过的错误守卫——它让每个已有 unique_id 的实体在第二次启动时都不被创建）
- **修复**：重新认证完成后条目自动重载（改用 `async_update_reload_and_abort`），无需重启 HA
- **修复**：选项（轮询间隔 / webhook 密钥）修改后立即生效（注册 `add_update_listener`，并用选项快照避免凭证续期触发重载死循环）
- **修复**：`cn_carrier_monitor.refresh` 服务不再因 `hass.data` 命名空间混用而报错（短信会话状态迁到独立键 `cn_carrier_monitor_sms_state`）
- **修复**：联通短信登录跨步骤丢失 `deviceId` 导致「验证码错误」；webhook 自动重登同样补上发码状态
- **修复**：电信滑块缺口定位（PIL 像素扫描）移入线程池，不再阻塞 HA 事件循环
- **安全**：出站 TLS 恢复证书校验；webhook 增加来源限制、常量时间密钥比较、验证码提交限频；`body.secret` 现在可用（原 schema 会拒收未知键）
- **稳定性**：自动重登发码改为指数退避（30 分钟起，翻倍封顶 6 小时），替代原 5 分钟冷却；多账号轮询加入随机抖动；移动网厅单接口漂移不再拖垮整次更新；电信会话失效判定收窄，避免业务提示误判为掉线
- **可观测**：诊断包新增 `last_error`（失败分类 + 摘要）与连续失败计数

## 已知限制与说明

- 运营商接口均为非公开接口，可能随官方升级而失效；本集成仅查询个人账号数据，请勿高频轮询（默认 60 分钟）。
- 电信登录有风控：频繁失败可能触发图形验证码或设备信任要求；建议刷新间隔 ≥ 30 分钟。
- **电信短信登录的 token 实测可用 ≥30 小时**（线上 81 次连续成功查询，见 [docs/会话生命周期语义表.md](docs/会话生命周期语义表.md) §2），此前文档"分钟级一次性"的结论已修正。但 token 失效后没有任何续期端点，仍需重登；有服务密码时优先用**服务密码**模式（可静默重登，无人值守）。
- 联通短信登录需要先过官方 H5 滑块（`m.client.10010.com/login-web`），可把页面返回的 `resultToken` 粘进配置流；账号被 `ECS1164` 风控时只能走服务密码或 mall Cookie。
- 移动查询走 `wx.10086.cn` 网厅通道（12.5 原生通道需在条目数据里显式 `use_native: true` 且使用 App 会话 Cookie）。**移动是单设备互斥**：手机端一打开中国移动 App 就会顶掉 HA 的会话，建议用备用机/模拟器抓包，抓完把主力机 App 划掉。
- 广电依赖手动导入会话，约 3 天重新导入一次。
- 阿里小号 / giffgaff 等虚拟运营商号码不支持。

## 架构

```
custom_components/cn_carrier_monitor/
├── api/            # 各运营商协议实现（可独立测试）
│   ├── crypto.py   # AES/RSA/JSONP/cURL 解析等公共工具
│   ├── base.py     # CarrierClient 基类 + 归一化数据结构
│   ├── telecom.py  # 189.cn 掌厅协议
│   ├── unicom.py   # m.client.10010.com App 协议 + 小程序链路
│   ├── mobile.py   # login.10086.cn 短信链路 + wx.10086.cn 查询
│   └── broadnet.py # 请求重放 + userResList 解析
├── coordinator.py  # 每账号 DataUpdateCoordinator
├── config_flow.py  # 分运营商分步配置 + reauth
├── sensor.py       # 实体平台
└── translations/   # zh-Hans / en
```

新增运营商只需实现 `CarrierClient`（`async_login` / `async_fetch`）并在 `api/__init__.py`、`config_flow.py` 注册。

## 免责声明

本项目仅供学习与个人账号自查使用，不构成对任何运营商服务的干扰；请控制查询频率，遵守当地法律法规。接口变更导致的不可用请提 issue。

## License

MIT
