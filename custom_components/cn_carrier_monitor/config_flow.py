"""Config flow for 中国运营商话费监控.

每个配置条目对应一个号码（多账号 = 多条目）。按运营商分步：
- 电信: 服务密码登录（登录即校验，token 持久化）
- 联通: 服务密码（App 协议）或小程序 openid 导入
- 移动: 短信验证码 / 服务密码 / Cookie 导入
- 广电: 浏览器会话导入（粘贴 qryUserRes 请求的 cURL 或 URL+Cookie）
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession

from .api import (
    BroadnetClient,
    CarrierAuthError,
    CarrierError,
    MobileClient,
    TelecomClient,
    UnicomClient,
    UnicomOpenIdClient,
    async_send_sms_code,
)
from .const import (
    AUTH_BROADNET_SESSION,
    AUTH_MOBILE_COOKIE,
    AUTH_MOBILE_PASSWORD,
    AUTH_MOBILE_SMS,
    AUTH_TELECOM_PASSWORD,
    AUTH_TELECOM_SMS,
    AUTH_UNICOM_OPENID,
    AUTH_UNICOM_PASSWORD,
    AUTH_UNICOM_SMS,
    CARRIER_BROADNET,
    CARRIER_LABELS,
    CARRIER_MOBILE,
    CARRIER_TELECOM,
    CARRIER_UNICOM,
    CONF_APP_ID,
    CONF_AUTH_MODE,
    CONF_CARRIER,
    CONF_CITY_CODE,
    CONF_COOKIES,
    CONF_DEVICE_ID,
    CONF_INTERVAL,
    CONF_OPENID,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_PROVINCE_CODE,
    CONF_REQUEST_BODY,
    CONF_REQUEST_METHOD,
    CONF_REQUEST_URL,
    CONF_TOKEN,
    DEFAULT_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PHONE_RE = r"^1[3-9]\d{9}$"


def _phone_schema(default: str = "") -> dict:
    return {
        vol.Required(CONF_PHONE, default=default): vol.All(str, vol.Match(PHONE_RE)),
    }


def _interval_schema(default: int = DEFAULT_INTERVAL) -> dict:
    return {
        vol.Optional(CONF_INTERVAL, default=default): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=1440)
        ),
    }


def _entry_title(carrier: str, phone: str) -> str:
    masked = f"{phone[:3]}****{phone[7:]}" if len(phone) == 11 else (phone or "未绑定")
    return f"{CARRIER_LABELS.get(carrier, carrier)} {masked}"


class ChinaCarrierMonitorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._carrier: str | None = None
        self._phone: str = ""
        self._sms_sent = False
        self._shared_session = None
        self._reauth_entry: config_entries.ConfigEntry | None = None

    def _session(self):
        if self._shared_session is None:
            self._shared_session = async_get_clientsession(self.hass)
        return self._shared_session

    # ------------------------------------------------------------------ user

    async def async_step_user(self, user_input=None):
        # REST/UI 直接以 source=reauth 发起的流程不会经过 async_step_reauth，
        # 从 flow context 里补解析目标条目
        if (
            self._reauth_entry is None
            and self.context.get("source") == "reauth"
            and self.context.get("entry_id")
        ):
            self._reauth_entry = self.hass.config_entries.async_get_entry(
                self.context["entry_id"]
            )
        if self._reauth_entry is not None:
            entry = self._reauth_entry
            self._carrier = entry.data[CONF_CARRIER]
            self._phone = entry.data.get(CONF_PHONE, "")
            mode = entry.data.get(CONF_AUTH_MODE, "")
            # 电信短信模式 reauth：手机号已知 → 自动发码直达输码步
            if (
                self._carrier == CARRIER_TELECOM
                and entry.data.get(CONF_AUTH_MODE) == AUTH_TELECOM_SMS
                and entry.data.get(CONF_PHONE)
            ):
                self._sms_sent = False
                session = async_create_clientsession(self.hass)
                sent = False
                try:
                    client = TelecomClient(session, dict(entry.data))
                    sent = await client.async_login_sms_send()
                except CarrierError as err:
                    _LOGGER.warning("电信 reauth 自动发码失败: %s", err)
                if sent:
                    self.hass.data.setdefault(DOMAIN, {}).setdefault(
                        "sms_state", {}
                    )[entry.data[CONF_PHONE]] = client.export_sms_state()
                    return await self.async_step_telecom_sms_code()
            route = {
                (CARRIER_TELECOM, AUTH_TELECOM_PASSWORD): "telecom",
                (CARRIER_TELECOM, AUTH_TELECOM_SMS): "telecom_sms",
                (CARRIER_UNICOM, AUTH_UNICOM_PASSWORD): "unicom",
                (CARRIER_UNICOM, AUTH_UNICOM_SMS): "unicom_sms",
                (CARRIER_UNICOM, AUTH_UNICOM_OPENID): "unicom_openid",
                (CARRIER_MOBILE, AUTH_MOBILE_SMS): "mobile_sms",
                (CARRIER_MOBILE, AUTH_MOBILE_PASSWORD): "mobile_password",
                (CARRIER_MOBILE, AUTH_MOBILE_COOKIE): "mobile_cookie",
                (CARRIER_BROADNET, AUTH_BROADNET_SESSION): "broadnet",
            }
            return await getattr(
                self, f"async_step_{route.get((self._carrier, mode), 'user')}"
            )()

        if user_input is not None:
            self._carrier = user_input[CONF_CARRIER]
            first_step = {
                CARRIER_TELECOM: "telecom_mode",
                CARRIER_UNICOM: "unicom_mode",
                CARRIER_MOBILE: "mobile_mode",
                CARRIER_BROADNET: "broadnet",
            }[self._carrier]
            return await getattr(self, f"async_step_{first_step}")()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_CARRIER, default=CARRIER_TELECOM): vol.In(CARRIER_LABELS)}
            ),
        )

    # ------------------------------------------------------------------ helpers

    async def _persist(self, data: dict, unique_phone: str | None = None):
        """Set the unique id and create the entry (or finish reauth)."""
        unique_id = f"{data[CONF_CARRIER]}_{unique_phone or data.get(CONF_PHONE, '')}"
        await self.async_set_unique_id(unique_id)
        if self._reauth_entry is not None:
            self._abort_if_unique_id_mismatch()
            self.hass.config_entries.async_update_entry(self._reauth_entry, data=data)
            return self.async_abort(reason="reauth_successful")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=_entry_title(data[CONF_CARRIER], data.get(CONF_PHONE, "")), data=data
        )

    # ------------------------------------------------------------------ telecom

    async def async_step_telecom(self, user_input=None):
        errors = {}
        placeholders = {}
        if user_input is not None:
            phone = user_input[CONF_PHONE]
            password = user_input[CONF_PASSWORD]
            session = async_create_clientsession(self.hass)
            try:
                client = TelecomClient(session, {"phone": phone, **user_input})
                creds = await client.async_login(phone, password)
                data = {
                    CONF_CARRIER: CARRIER_TELECOM,
                    CONF_AUTH_MODE: AUTH_TELECOM_PASSWORD,
                    CONF_PHONE: phone,
                    CONF_PASSWORD: password,
                    CONF_DEVICE_ID: user_input.get(CONF_DEVICE_ID, ""),
                    CONF_TOKEN: creds["token"],
                    CONF_PROVINCE_CODE: creds["province_code"],
                    CONF_CITY_CODE: creds["city_code"],
                    CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
                }
            except CarrierAuthError as err:
                errors["base"] = str(err) if err.args else "telecom_invalid_credentials"
                placeholders["reason"] = err.detail or ""
            except CarrierError as err:
                errors["base"] = "cannot_connect"
                placeholders["reason"] = getattr(err, "detail", "") or str(err)[:120]
                _LOGGER.warning("电信登录失败: %s", err)
            finally:
                pass  # HA-managed session: do not close
            if not errors:
                return await self._persist(data)
        return self.async_show_form(
            step_id="telecom",
            data_schema=vol.Schema(
                {
                    **_phone_schema(self._phone if errors else ""),
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_DEVICE_ID, default=""): str,
                    **_interval_schema(),
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )


    async def async_step_telecom_mode(self, user_input=None):
        """电信登录方式选择：短信 / 服务密码。"""
        if user_input is not None:
            return await (
                self.async_step_telecom_sms_phone()
                if user_input["mode"] == AUTH_TELECOM_SMS
                else self.async_step_telecom()
            )
        return self.async_show_form(
            step_id="telecom_mode",
            data_schema=vol.Schema(
                {
                    vol.Required("mode", default=AUTH_TELECOM_SMS): vol.In(
                        {
                            AUTH_TELECOM_SMS: "短信验证码登录（推荐，自动过滑块）",
                            AUTH_TELECOM_PASSWORD: "服务密码登录",
                        }
                    )
                }
            ),
        )

    async def async_step_telecom_sms_phone(self, user_input=None):
        """电信短信登录第一步：填手机号并发码（自动过滑块）。"""
        errors = {}
        if user_input is not None:
            self._phone = user_input[CONF_PHONE]
            session = async_create_clientsession(self.hass)
            sent = False
            try:
                client = TelecomClient(session, {"phone": self._phone})
                sent = await client.async_login_sms_send()
            except CarrierAuthError as err:
                errors["base"] = str(err) if err.args else "telecom_sms_send_failed"
            except CarrierError as err:
                errors["base"] = "telecom_sms_send_failed"
                _LOGGER.warning("电信短信下发失败: %s | %s", err, getattr(err, "detail", ""))
            finally:
                pass  # HA-managed session: do not close
            if sent:
                self.hass.data.setdefault(DOMAIN, {}).setdefault(
                    "sms_state", {}
                )[self._phone] = client.export_sms_state()
                return await self.async_step_telecom_sms_code()
        return self.async_show_form(
            step_id="telecom_sms_phone",
            data_schema=vol.Schema(_phone_schema(self._phone)),
            errors=errors,
        )

    async def async_step_telecom_sms_code(self, user_input=None):
        """电信短信登录第二步：输入验证码。"""
        errors = {}
        if user_input is not None:
            code = user_input["code"].strip()
            session = async_create_clientsession(self.hass)
            data = {}
            try:
                client = TelecomClient(session, {"phone": self._phone})
                saved_state = (
                    self.hass.data.get(DOMAIN, {}).get("sms_state", {}).get(self._phone)
                )
                if saved_state:
                    client.import_sms_state(saved_state)
                creds = await client.async_login_sms_submit(code)
                data = {
                    CONF_CARRIER: CARRIER_TELECOM,
                    CONF_AUTH_MODE: AUTH_TELECOM_SMS,
                    CONF_PHONE: self._phone,
                    CONF_PASSWORD: "",
                    CONF_TOKEN: creds["token"],
                    CONF_PROVINCE_CODE: creds["province_code"],
                    CONF_CITY_CODE: creds["city_code"],
                    CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
                }
            except CarrierAuthError as err:
                errors["base"] = str(err) if err.args else "telecom_sms_login_failed"
                if getattr(err, "detail", ""):
                    _LOGGER.warning("电信短信登录失败: %s", err.detail)
            except CarrierError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("电信短信登录异常: %s", err)
            finally:
                pass  # HA-managed session: do not close
            if not errors:
                return await self._persist(data)
        return self.async_show_form(
            step_id="telecom_sms_code",
            data_schema=vol.Schema(
                {
                    vol.Required("code"): vol.All(str, vol.Match(r"^\d{4,8}$")),
                    **_interval_schema(),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------ unicom

    async def async_step_unicom_mode(self, user_input=None):
        if user_input is not None:
            return await {
                AUTH_UNICOM_SMS: self.async_step_unicom_sms,
                AUTH_UNICOM_PASSWORD: self.async_step_unicom,
                AUTH_UNICOM_OPENID: self.async_step_unicom_openid,
            }[user_input["mode"]]()
        return self.async_show_form(
            step_id="unicom_mode",
            data_schema=vol.Schema(
                {
                    vol.Required("mode", default=AUTH_UNICOM_SMS): vol.In(
                        {
                            AUTH_UNICOM_SMS: "短信验证码登录（推荐）",
                            AUTH_UNICOM_PASSWORD: "服务密码登录",
                            AUTH_UNICOM_OPENID: "小程序 openid 导入",
                        }
                    )
                }
            ),
        )

    async def async_step_unicom(self, user_input=None):
        errors = {}
        placeholders = {}
        data = {}
        if user_input is not None:
            phone = user_input[CONF_PHONE]
            password = user_input[CONF_PASSWORD]
            session = async_create_clientsession(self.hass)
            try:
                client = UnicomClient(session, {"phone": phone, **user_input})
                creds = await client.async_login(phone, password)
                data = {
                    CONF_CARRIER: CARRIER_UNICOM,
                    CONF_AUTH_MODE: AUTH_UNICOM_PASSWORD,
                    CONF_PHONE: phone,
                    CONF_PASSWORD: password,
                    CONF_APP_ID: creds["app_id"],
                    CONF_TOKEN: creds["token"],
                    CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
                }
            except CarrierAuthError as err:
                errors["base"] = str(err) if err.args else "unicom_invalid_credentials"
                placeholders["reason"] = err.detail or ""
            except CarrierError as err:
                errors["base"] = "unicom_login_failed" if getattr(err, "detail", "") else "cannot_connect"
                placeholders["reason"] = getattr(err, "detail", "") or str(err)[:120]
                _LOGGER.warning("联通登录失败: %s", err)
            finally:
                pass  # HA-managed session: do not close
            if not errors:
                return await self._persist(data)
        return self.async_show_form(
            step_id="unicom",
            data_schema=vol.Schema(
                {
                    **_phone_schema(self._phone if errors else ""),
                    vol.Required(CONF_PASSWORD): str,
                    **_interval_schema(),
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_unicom_openid(self, user_input=None):
        errors = {}
        data = {}
        unique_phone = None
        if user_input is not None:
            openid = user_input[CONF_OPENID].strip()
            session = async_create_clientsession(self.hass)
            try:
                client = UnicomOpenIdClient(session, {"openid": openid})
                snapshot = await client.async_fetch()
                unique_phone = str(snapshot.get("phone") or "") or None
                data = {
                    CONF_CARRIER: CARRIER_UNICOM,
                    CONF_AUTH_MODE: AUTH_UNICOM_OPENID,
                    CONF_PHONE: user_input.get(CONF_PHONE) or unique_phone or "",
                    CONF_OPENID: openid,
                    CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
                }
            except CarrierAuthError:
                errors["base"] = "unicom_openid_invalid"
            except CarrierError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("联通 openid 校验失败: %s", err)
            finally:
                pass  # HA-managed session: do not close
            if not errors:
                return await self._persist(
                    data, unique_phone=unique_phone or data.get(CONF_PHONE) or None
                )
        return self.async_show_form(
            step_id="unicom_openid",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_OPENID): str,
                    vol.Optional(CONF_PHONE, default=self._phone): str,
                    **_interval_schema(),
                }
            ),
            errors=errors,
        )


    async def async_step_unicom_sms(self, user_input=None):
        """联通短信登录：手机号 + （可选）H5 滑块 resultToken。"""
        errors = {}
        placeholders = {}
        if user_input is not None:
            self._phone = user_input[CONF_PHONE]
            result_token = user_input.get("result_token", "").strip()
            session = async_create_clientsession(self.hass)
            hint = ""
            try:
                client = UnicomClient(session, {"phone": self._phone})
                if result_token:
                    await client.async_sms_send_with_token(result_token)
                    hint = "短信验证码已下发，请查收后继续"
                else:
                    hint = await client.async_sms_trigger()
                return await self.async_step_unicom_sms_code(hint)
            except CarrierError as err:
                errors["base"] = "unicom_sms_send_failed"
                placeholders["reason"] = getattr(err, "detail", "") or str(err)[:120]
                _LOGGER.warning("联通短信下发失败: %s", err)
            finally:
                pass  # HA-managed session: do not close
        placeholders.setdefault("reason", "")
        return self.async_show_form(
            step_id="unicom_sms",
            data_schema=vol.Schema(
                {
                    **_phone_schema(self._phone),
                    vol.Optional("result_token", default=""): str,
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_unicom_sms_code(self, hint="", user_input=None):
        """联通短信登录第二步：输入验证码。"""
        errors = {}
        if user_input is not None:
            code = user_input["code"].strip()
            session = async_create_clientsession(self.hass)
            data = {}
            try:
                client = UnicomClient(session, {"phone": self._phone})
                creds = await client.async_sms_login(code)
                data = {
                    CONF_CARRIER: CARRIER_UNICOM,
                    CONF_AUTH_MODE: AUTH_UNICOM_SMS,
                    CONF_PHONE: self._phone,
                    CONF_PASSWORD: "",
                    CONF_APP_ID: creds["app_id"],
                    CONF_TOKEN: creds["token"],
                    CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
                }
            except CarrierAuthError as err:
                errors["base"] = str(err) if err.args else "unicom_sms_login_failed"
            except CarrierError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("联通短信登录异常: %s", err)
            finally:
                pass  # HA-managed session: do not close
            if not errors:
                return await self._persist(data)
        return self.async_show_form(
            step_id="unicom_sms_code",
            data_schema=vol.Schema(
                {
                    vol.Required("code"): vol.All(str, vol.Match(r"^\d{4,8}$")),
                    **_interval_schema(),
                }
            ),
            errors=errors,
            description_placeholders={"hint": hint},
        )

    # ------------------------------------------------------------------ mobile

    async def async_step_mobile_mode(self, user_input=None):
        if user_input is not None:
            return await {
                AUTH_MOBILE_SMS: self.async_step_mobile_sms,
                AUTH_MOBILE_PASSWORD: self.async_step_mobile_password,
                AUTH_MOBILE_COOKIE: self.async_step_mobile_cookie,
            }[user_input["mode"]]()
        return self.async_show_form(
            step_id="mobile_mode",
            data_schema=vol.Schema(
                {
                    vol.Required("mode", default=AUTH_MOBILE_SMS): vol.In(
                        {
                            AUTH_MOBILE_SMS: "短信验证码登录（推荐）",
                            AUTH_MOBILE_PASSWORD: "服务密码登录",
                            AUTH_MOBILE_COOKIE: "粘贴浏览器 Cookie",
                        }
                    )
                }
            ),
        )

    async def async_step_mobile_sms(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._phone = user_input[CONF_PHONE]
            if not self._sms_sent:
                # 服务端代发常被移动风控拒绝（官方要求发码发生在用户侧网络），
                # 失败不阻断：进入输码步，引导用户在官方渠道触发验证码
                try:
                    await async_send_sms_code(self._session(), self._phone)
                except CarrierError as err:
                    _LOGGER.info("移动服务端代发未成功（改为用户侧取码）: %s", err)
            return await self.async_step_mobile_sms_code()
        return self.async_show_form(
            step_id="mobile_sms",
            data_schema=vol.Schema(_phone_schema(self._phone)),
            errors=errors,
        )

    async def async_step_mobile_sms_code(self, user_input=None):
        errors = {}
        data = {}
        detected_phone = None
        if user_input is not None:
            code = user_input["code"].strip()
            session = async_create_clientsession(self.hass)
            try:
                client = MobileClient(
                    session, {CONF_PHONE: self._phone, CONF_AUTH_MODE: AUTH_MOBILE_SMS}
                )
                await client.async_submit_code(self._phone, code)
                snapshot = await client.async_fetch()
                detected_phone = str(snapshot.get("phone") or "") or None
                data = {
                    CONF_CARRIER: CARRIER_MOBILE,
                    CONF_AUTH_MODE: AUTH_MOBILE_SMS,
                    CONF_PHONE: self._phone,
                    CONF_DEVICE_ID: client.export_credentials().get("device_id", ""),
                    CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
                }
            except CarrierAuthError as err:
                errors["base"] = str(err) if err.args else "mobile_code_invalid"
            except CarrierError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("移动登录/校验失败: %s", err)
            finally:
                pass  # HA-managed session: do not close
            if not errors:
                return await self._persist(data, unique_phone=detected_phone)
        return self.async_show_form(
            step_id="mobile_sms_code",
            data_schema=vol.Schema(
                {
                    vol.Required("code"): vol.All(str, vol.Match(r"^\d{4,8}$")),
                    **_interval_schema(),
                }
            ),
            errors=errors,
        )

    async def async_step_mobile_password(self, user_input=None):
        errors = {}
        data = {}
        if user_input is not None:
            phone = user_input[CONF_PHONE]
            password = user_input[CONF_PASSWORD]
            session = async_create_clientsession(self.hass)
            try:
                client = MobileClient(
                    session, {"phone": phone, CONF_AUTH_MODE: AUTH_MOBILE_PASSWORD}
                )
                await client.async_login_password(phone, password)
                await client.async_fetch()
                data = {
                    CONF_CARRIER: CARRIER_MOBILE,
                    CONF_AUTH_MODE: AUTH_MOBILE_PASSWORD,
                    CONF_PHONE: phone,
                    CONF_PASSWORD: password,
                    CONF_DEVICE_ID: client.export_credentials().get("device_id", ""),
                    CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
                }
            except CarrierAuthError as err:
                errors["base"] = str(err) if err.args else "mobile_password_error"
            except CarrierError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("移动密码登录失败: %s", err)
            finally:
                pass  # HA-managed session: do not close
            if not errors:
                return await self._persist(data)
        return self.async_show_form(
            step_id="mobile_password",
            data_schema=vol.Schema(
                {
                    **_phone_schema(self._phone),
                    vol.Required(CONF_PASSWORD): str,
                    **_interval_schema(),
                }
            ),
            errors=errors,
        )

    async def async_step_mobile_cookie(self, user_input=None):
        errors = {}
        data = {}
        detected_phone = None
        if user_input is not None:
            phone = user_input[CONF_PHONE]
            cookie = user_input[CONF_COOKIES].strip()
            session = async_create_clientsession(self.hass)
            try:
                client = MobileClient(
                    session,
                    {"phone": phone, CONF_AUTH_MODE: AUTH_MOBILE_COOKIE, CONF_COOKIES: cookie},
                )
                snapshot = await client.async_fetch()
                detected_phone = str(snapshot.get("phone") or "") or None
                data = {
                    CONF_CARRIER: CARRIER_MOBILE,
                    CONF_AUTH_MODE: AUTH_MOBILE_COOKIE,
                    CONF_PHONE: phone,
                    CONF_COOKIES: cookie,
                    CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
                }
            except CarrierAuthError:
                errors["base"] = "mobile_session_expired"
            except CarrierError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("移动 Cookie 校验失败: %s", err)
            finally:
                pass  # HA-managed session: do not close
            if not errors:
                return await self._persist(data, unique_phone=detected_phone)
        return self.async_show_form(
            step_id="mobile_cookie",
            data_schema=vol.Schema(
                {
                    **_phone_schema(self._phone),
                    vol.Required(CONF_COOKIES): str,
                    **_interval_schema(),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------ broadnet

    async def async_step_broadnet(self, user_input=None):
        errors = {}
        data = {}
        if user_input is not None:
            source = user_input.get("request_source", "").strip()
            cookie = user_input.get(CONF_COOKIES, "").strip()
            session = async_create_clientsession(self.hass)
            normalized = None
            try:
                normalized = BroadnetClient.parse_import(source, cookie)
            except CarrierError as err:
                errors["base"] = "broadnet_import_invalid"
                _LOGGER.warning("广电会话导入解析失败: %s", err)
            if normalized is not None:
                try:
                    phone = user_input.get(CONF_PHONE) or ""
                    client = BroadnetClient(session, {"phone": phone, **normalized})
                    await client.async_fetch()
                    data = {
                        CONF_CARRIER: CARRIER_BROADNET,
                        CONF_AUTH_MODE: AUTH_BROADNET_SESSION,
                        CONF_PHONE: phone,
                        **normalized,
                        CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
                    }
                except CarrierAuthError:
                    errors["base"] = "broadnet_session_expired"
                except CarrierError as err:
                    errors["base"] = "broadnet_query_failed"
                    _LOGGER.warning("广电查询失败: %s", err)
                finally:
                    pass  # HA-managed session: do not close
            if not errors and data:
                return await self._persist(data)
        return self.async_show_form(
            step_id="broadnet",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PHONE, default=self._phone): str,
                    vol.Required("request_source"): str,
                    vol.Optional(CONF_COOKIES, default=""): str,
                    **_interval_schema(),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------ reauth

    async def async_step_reauth(self, entry_data):
        """Entry point raised via ConfigEntryAuthFailed."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_user(None)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ChinaCarrierMonitorOptionsFlow(config_entry)


class ChinaCarrierMonitorOptionsFlow(config_entries.OptionsFlowWithConfigEntry):
    """Options: refresh interval."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options.get(
            CONF_INTERVAL,
            self.config_entry.data.get(CONF_INTERVAL, DEFAULT_INTERVAL),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(_interval_schema(current)),
        )
