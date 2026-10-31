
/**
 * cn_carrier_monitor 集成图标覆盖
 * brands PR 合并前的前端方案：拦截 brands 404，替换为集成自带图标
 * 合并后此文件自动失效（brands URL 返回 200 不触发覆盖）
 */
(async () => {
  const DOMAIN = "cn_carrier_monitor";
  const BRAND_BASE = "https://brands.home-assistant.io/" + DOMAIN;
  const LOCAL_BASE = "/api/custom_components/" + DOMAIN + "/images";

  // 检测 brands 图标是否可用
  async function checkUrl(url) {
    try {
      const r = await fetch(url, { method: "HEAD" });
      return r.ok;
    } catch { return false; }
  }

  // 替换所有 img 标签中的 brands 404 图标
  function fixIcons() {
    document.querySelectorAll('img[src*="brands.home-assistant.io"]').forEach(img => {
      if (img.src.includes(DOMAIN) && img.naturalWidth === 0) {
        const kind = img.src.includes("logo") ? "logo" : "icon";
        const dark = img.src.includes("dark") ? "dark_" : "";
        const size = img.src.includes("@2x") ? "@2x" : "";
        img.src = `${LOCAL_BASE}/${dark}${kind}${size}.png`;
        img.onerror = () => { img.style.display = "none"; };
      }
    });
  }

  // 等待 DOM 后执行 + MutationObserver 监听动态内容
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { fixIcons(); new MutationObserver(fixIcons).observe(document.body, { childList: true, subtree: true }); });
  } else {
    fixIcons();
    new MutationObserver(fixIcons).observe(document.body, { childList: true, subtree: true });
  }
})();
