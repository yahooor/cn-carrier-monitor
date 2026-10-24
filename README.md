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
- **自动重新认证**：会话失效时自动发起 reauth 配置流，通知你重新登录
- **服务**：`cn_carrier_monitor.refresh` 手动刷新（可指定 entry_id）
- HACS 可装；中文界面

## 安装

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

## 已知限制与说明

- 运营商接口均为非公开接口，可能随官方升级而失效；本集成仅查询个人账号数据，请勿高频轮询（默认 60 分钟）。
- 电信登录有风控：频繁失败可能触发图形验证码或设备信任要求；建议刷新间隔 ≥ 30 分钟。
- 联通「短信验证码登录」暂不支持（App 短信登录端点无公开稳定实现），提供 openid 兜底。
- 移动短信链路中发送验证码端点 (`sendRandomCodeAction.action`) 的参数来自社区逆向，若发送失败请改用服务密码或 Cookie 方式。
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
