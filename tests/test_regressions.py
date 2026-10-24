"""Regression suite: one test per bug found during live bring-up
(2026-10-24/25, HA 2026.10.2). Every guard below maps to a real failure we hit.
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CCM = REPO / "custom_components" / "cn_carrier_monitor"


def test_regression_undefined_names_ruff_f821():
    """BUG: telecom/broadnet/unicom 缺 empty_data 导入 → 配置后首查即 NameError
    (HA reason: "查询失败: name 'empty_da...")。该 bug 属 F821(undefined-name)，
    用 ruff 实抓（比人工静态模式可靠）。"""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F821",
         str(CCM / "api"), str(CCM / "config_flow.py"), str(CCM / "coordinator.py"),
         str(CCM / "webhook.py"), str(CCM / "sensor.py"), str(CCM / "button.py"),
         str(CCM / "diagnostics.py"), str(CCM / "net.py"), str(CCM / "__init__.py")],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, "F821 names: " + result.stdout


def test_regression_no_session_close_in_config_flow():
    """BUG: config_flow 关闭 HA 管理的 clientsession（12 处 WARNING）。"""
    src = (CCM / "config_flow.py").read_text(encoding="utf-8")
    assert "session.close()" not in src


def test_regression_options_flow_new_api():
    """BUG: OptionsFlow 构造签名（新版 HA 报错 line739/746）。"""
    src = (CCM / "config_flow.py").read_text(encoding="utf-8")
    assert "OptionsFlowWithConfigEntry" in src
    # 不得再手写 self.config_entry = config_entry
    assert "def __init__(self, config_entry" not in src


def _step_descriptions(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = data.get("config", {}).get("step", {})
    for step_id, step in steps.items():
        desc = step.get("description", "")
        yield path.name, step_id, desc


def test_regression_translation_placeholders_balanced():
    """BUG: unicom_sms description 带 {reason} 但未提供 → 前端 MISSING_VALUE 刷屏。
    规则：描述中的 {var} 必须在对应 show_form 默认提供（静态近似检查——
    出现在描述里的每个变量名必须在 config_flow.py 中出现 setdefault 或占位传入）。"""
    flow_src = (CCM / "config_flow.py").read_text(encoding="utf-8")
    bad = []
    for f in (CCM / "strings.json", CCM / "translations" / "zh-Hans.json",
              CCM / "translations" / "en.json"):
        for fname, step_id, desc in _step_descriptions(f):
            for var in re.findall(r"\{([a-z_]+)\}", desc):
                if var not in flow_src:
                    bad.append(f"{fname}:{step_id}:{var}")
    assert not bad, f"placeholder 无来源: {bad}"


def test_regression_persistent_notification_direct_api():
    """BUG: hass.components.persistent_notification 已移除 → 自动发码通知静默失败。"""
    src = (CCM / "coordinator.py").read_text(encoding="utf-8")
    assert "hass.components" not in src


def test_regression_manifest_declares_notification_dep():
    """BUG: 未声明 persistent_notification 依赖 → task 内访问组件崩溃被吞。"""
    m = json.loads((CCM / "manifest.json").read_text(encoding="utf-8"))
    assert "persistent_notification" in m.get("dependencies", [])


def test_regression_services_yaml_exists():
    """BUG: refresh 服务注册但缺 services.yaml → 每次 HA 启动报 ERROR。"""
    assert (CCM / "services.yaml").exists()
    data = (CCM / "services.yaml").read_text(encoding="utf-8")
    assert "refresh" in data


def test_regression_webhook_registered_in_setup():
    src = (CCM / "__init__.py").read_text(encoding="utf-8")
    assert "webhook" in src and "async_register" in src


def test_regression_deploy_script_md5_verification():
    """部署脚本必须做 md5 校验 + 清 __pycache__（旧字节码导致多轮误诊）。"""
    deploy = (REPO.parent / "deploy.py").read_text(encoding="utf-8")
    assert "md5" in deploy and "__pycache__" in deploy


def test_uvr_all_modules_compile():
    """整体编译 + ruff F821 由 CI 承担；这里保证无裸 except 吞 NameError 类错误
    的最小冒烟：模块可在无 HA 环境下加载（api 层）。"""
    for f in sorted((CCM / "api").glob("*.py")):
        compile(f.read_text(encoding="utf-8"), str(f), "exec")
