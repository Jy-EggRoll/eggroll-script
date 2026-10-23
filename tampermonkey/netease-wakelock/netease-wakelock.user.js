// ==UserScript==
// @name         网易云网页版防息屏
// @namespace    local.eggroll.webplayer
// @version      0.1.0
// @description  在 music.163.com 播放器页面上提供防息屏开关：开启后页面可见期间持有屏幕唤醒锁，切后台自动释放、回前台自动重取
// @match        https://music.163.com/st/webplayer*
// @match        https://music.163.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * 为什么这样设计（与 solara 的 wake-lock.js 同一套语义）：
 * - 用户手动开启后，只要停留在页面且页面可见，就一直持有屏幕锁，与播放/暂停无关；
 *   仅当关闭开关、切到后台或离开页面时释放。浏览器在页面隐藏时会自动释放，回到前台重取。
 * - Wake Lock 只能阻止显示器关闭/锁定，不能阻止系统睡眠与合盖休眠（这是操作系统电源管理的事）。
 * - @grant none：本脚本只用标准 API（navigator.wakeLock、localStorage、DOM），不需要 GM_* 函数。
 *   注意 @grant none 会关闭篡改猴沙箱，脚本直接运行在页面上下文，本脚本不读写页面私有变量，可接受。
 */

(function () {
    "use strict";

    const STORAGE_KEY = "ncm_webplayer_wakelock";
    const LOG_PREFIX = "[防息屏]";

    let sentinel = null;
    let enabled = false;

    function log(level, message) {
        // 原型阶段统一走 console.debug，便于在页面控制台按级别过滤
        console[level](`${LOG_PREFIX} ${message}`);
    }

    function isSupported() {
        return (
            typeof navigator !== "undefined" &&
            "wakeLock" in navigator &&
            typeof window !== "undefined" &&
            window.isSecureContext === true
        );
    }

    async function release() {
        if (!sentinel) return;
        try {
            await sentinel.release();
            log("debug", "屏幕锁已释放");
        } catch (_) {
            // 已被浏览器释放（切后台/锁屏），忽略
        }
        sentinel = null;
    }

    async function acquire() {
        if (!enabled || !isSupported()) return;
        if (document.hidden) return;
        try {
            await release();
            sentinel = await navigator.wakeLock.request("screen");
            sentinel.addEventListener(
                "release",
                () => {
                    sentinel = null;
                    log("debug", "屏幕锁已被系统释放（切后台/锁屏）");
                },
                { once: true },
            );
            log("debug", "屏幕锁已获取");
        } catch (err) {
            sentinel = null;
            log("warn", `获取屏幕锁失败: ${err && err.name ? err.name : err}`);
        }
    }

    async function refresh() {
        if (enabled && !document.hidden) {
            await acquire();
        } else {
            await release();
        }
        render();
    }

    function render() {
        const btn = document.getElementById("ncm-wakelock-btn");
        if (!btn) return;
        const label = `防息屏：${enabled ? "开" : "关"}`;
        btn.textContent = label;
        btn.classList.toggle("is-active", enabled);
        btn.setAttribute("aria-pressed", enabled ? "true" : "false");
        btn.title = enabled ? "屏幕将保持常亮（切后台自动释放）" : "开启后屏幕不会自动熄灭";
    }

    function toggle() {
        enabled = !enabled;
        try {
            localStorage.setItem(STORAGE_KEY, enabled ? "1" : "0");
        } catch (_) {
            // 隐私模式等场景下 localStorage 可能不可用，开关仍然生效但不持久化
        }
        log("info", `开关切换为：${enabled ? "开" : "关"}`);
        refresh();
    }

    function createButton() {
        // 网易云页面有自己的样式体系，这里用内联样式做最小侵入，避免依赖站点类名
        const btn = document.createElement("button");
        btn.id = "ncm-wakelock-btn";
        btn.type = "button";
        btn.style.cssText = [
            "position:fixed",
            "right:12px",
            "bottom:96px",
            "z-index:99999",
            "padding:6px 12px",
            "border-radius:14px",
            "border:1px solid rgba(255,255,255,.35)",
            "background:rgba(0,0,0,.55)",
            "color:#fff",
            "font-size:12px",
            "cursor:pointer",
            "opacity:.75",
        ].join(";");
        btn.addEventListener("mouseenter", () => (btn.style.opacity = "1"));
        btn.addEventListener("mouseleave", () => (btn.style.opacity = ".75"));
        btn.addEventListener("click", toggle);
        document.body.appendChild(btn);
        return btn;
    }

    function init() {
        if (!isSupported()) {
            log("warn", `不可用：wakeLock 支持=${"wakeLock" in navigator}，安全上下文=${window.isSecureContext}`);
            return;
        }
        try {
            enabled = localStorage.getItem(STORAGE_KEY) === "1";
        } catch (_) {
            enabled = false;
        }
        createButton();
        render();
        document.addEventListener("visibilitychange", () => {
            refresh();
        });
        if (enabled) refresh();
        log("info", `初始化完成：可用，当前开关=${enabled ? "开" : "关"}`);
    }

    init();
})();