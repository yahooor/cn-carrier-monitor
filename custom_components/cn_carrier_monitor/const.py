"""Constants for the 中国运营商话费监控 (China Carrier Monitor) integration."""

DOMAIN = "cn_carrier_monitor"
NAME = "中国运营商话费监控"
MANUFACTURER = "China Carrier Monitor"

PLATFORMS = ["sensor"]

# ---------------------------------------------------------------- carriers
CARRIER_TELECOM = "telecom"    # 中国电信
CARRIER_UNICOM = "unicom"      # 中国联通
CARRIER_MOBILE = "mobile"      # 中国移动
CARRIER_BROADNET = "broadnet"  # 中国广电

CARRIER_LIST = [CARRIER_TELECOM, CARRIER_UNICOM, CARRIER_MOBILE, CARRIER_BROADNET]

CARRIER_LABELS = {
    CARRIER_TELECOM: "中国电信",
    CARRIER_UNICOM: "中国联通",
    CARRIER_MOBILE: "中国移动",
    CARRIER_BROADNET: "中国广电",
}

# ---------------------------------------------------------------- config keys
CONF_CARRIER = "carrier"
CONF_PHONE = "phone"
CONF_PASSWORD = "password"
CONF_OPENID = "openid"
CONF_INTERVAL = "interval"          # minutes
CONF_DEVICE_ID = "device_id"        # telecom trusted device id (optional)
CONF_AUTH_MODE = "auth_mode"        # per-carrier auth mode
CONF_COOKIES = "cookies"            # mobile / broadnet pasted cookie header
CONF_REQUEST_URL = "request_url"    # broadnet replayed qryUserRes request URL
CONF_REQUEST_METHOD = "request_method"
CONF_REQUEST_BODY = "request_body"  # broadnet replayed body (optional)
CONF_TOKEN = "token"                # telecom token / unicom token_online
CONF_PROVINCE_CODE = "province_code"
CONF_CITY_CODE = "city_code"
CONF_APP_ID = "app_id"              # unicom device-stable appId

AUTH_TELECOM_PASSWORD = "password"
AUTH_TELECOM_SMS = "sms"
AUTH_UNICOM_PASSWORD = "password"
AUTH_UNICOM_SMS = "sms"
AUTH_UNICOM_OPENID = "openid"
AUTH_MOBILE_SMS = "sms"
AUTH_MOBILE_PASSWORD = "password"
AUTH_MOBILE_COOKIE = "cookie"
AUTH_BROADNET_SESSION = "session"

DEFAULT_INTERVAL = 60  # minutes

# ---------------------------------------------------------------- endpoints: telecom
TELECOM_LOGIN_URL = "https://appgologin.189.cn:9031/login/client/userLoginNormal"
# Pocket-Carrier 新一代登录族接口（滑块/短信验证码，source=120002）
TELECOM_LOGIN_V2_URL = "https://appgologinsz.189.cn/login/client"
# 同集群回退域名（部分内网 DNS 解析不到 sz 域名）
TELECOM_LOGIN_V2_HOSTS = [
    "https://appgologinsz.189.cn",
    "https://appgologin.189.cn:9031",
]
TELECOM_QUERY_URL = "https://appfuwu.189.cn:9021/query/qryImportantData"
TELECOM_FLUX_URL = "https://appfuwu.189.cn:9021/query/userFluxPackage"
TELECOM_UA = "P216010901"
TELECOM_CLIENT_TYPE = "#12.2.0#channel50#iPhone 14 Pro#"
TELECOM_SHOP_ID = "20002"
TELECOM_SOURCE = "110003"
TELECOM_SOURCE_PASSWORD = "Sid98s"

# ---------------------------------------------------------------- endpoints: unicom
UNICOM_LOGIN_URL = "https://m.client.10010.com/mobileService/login.htm"
UNICOM_ONLINE_URL = "https://m.client.10010.com/mobileService/onLine.htm"
UNICOM_PACKAGE_URL = (
    "https://m.client.10010.com/servicequerybusiness/operationservice/"
    "queryOcsPackageFlowLeftContentRevisedInJune"
)
UNICOM_BALANCE_URL = (
    "https://m.client.10010.com/servicequerybusiness/balancenew/accountBalancenew.htm"
)
UNICOM_VERSION = "android@11.0900"
UNICOM_UA = "unicom{version:android@11.0900}"

# unicom WeChat mini program (openid fallback)
UNICOM_MINI_BASE = "https://mina.10010.com/wxapplet/weixinNew/"
UNICOM_MINI_GET_TOKEN = UNICOM_MINI_BASE + "getToken"
UNICOM_MINI_OVERVIEW = UNICOM_MINI_BASE + "sspbigball"
UNICOM_MINI_GOODS = UNICOM_MINI_BASE + "queryGoodsList"
UNICOM_MINI_BALANCE_BROADCAST = UNICOM_MINI_BASE + "sspbalcbroadcast"
UNICOM_MINI_GET_TICKET = UNICOM_MINI_BASE + "getTicket"
UNICOM_MINI_SERVICE_ENTRANCE = (
    "https://mxx.client.10010.com/servicebusiness/wx/serviceEntrance"
)
UNICOM_MINI_FLOW_URL = (
    "https://mxx.client.10010.com/servicequerybusiness/operationservice/"
    "queryOcsPackageFlowLeftContentRevisedInJune"
)
UNICOM_MINI_APPID = "wx56af9763578b9a93"

# ---------------------------------------------------------------- endpoints: mobile
MOBILE_LOGIN_HOST = "https://login.10086.cn"
MOBILE_LOGIN_PAGE = (
    MOBILE_LOGIN_HOST
    + "/login.html?channelID=12003&backUrl=https%3A%2F%2Fshop.10086.cn%2Fi%2F%3Ff%3Dhome"
)
MOBILE_LOAD_TOKEN_URL = MOBILE_LOGIN_HOST + "/loadToken.action"
MOBILE_SEND_SMS_URL = MOBILE_LOGIN_HOST + "/sendRandomCodeAction.action"
MOBILE_LOGIN_URL = MOBILE_LOGIN_HOST + "/login.htm"
MOBILE_CHANNEL_ID = "12003"
MOBILE_BACK_URL = "https://shop.10086.cn/i/?f=home"

MOBILE_FEE_URL = "https://wx.10086.cn/website/serviceMargin/fareBalance"
MOBILE_PLAN_URL = "https://wx.10086.cn/website/serviceMargin/getMainPlan"
MOBILE_MARGIN_URL = "https://wx.10086.cn/website/serviceMargin/getNewMarginInfo"
MOBILE_QUERY_REFERER = "https://wx.10086.cn/website/spa/main/newHome"

# ---------------------------------------------------------------- misc
REQUEST_TIMEOUT = 30
SMS_TIMEOUT = 60

# flow unit codes used by mobile getNewMarginInfo
MOBILE_UNIT_MAP = {"01": "min", "02": "sms", "03": "MB", "04": "GB"}

ATTR_LAST_UPDATE = "last_update"
ATTR_PHONE = "phone"
ATTR_CARRIER = "carrier"

SERVICE_REFRESH = "refresh"
