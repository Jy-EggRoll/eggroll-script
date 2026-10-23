// ==UserScript==
// @name         网易云网页版防息屏
// @namespace    local.eggroll.webplayer
// @version      0.1.2
// @description  在 music.163.com 播放器页面上提供防息屏开关：开启后页面可见期间持有屏幕唤醒锁，切后台自动释放、回前台自动重取
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
    // 最近一次申请失败的原因。开关因此回退为关时靠它把原因显示在按钮上，避免静默失败
    let errorHint = "";

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
        const lock = sentinel;
        if (!lock) return;
        // 先清空引用再等待释放完成：释放过程中再进入本函数时不会重复释放同一把锁
        sentinel = null;
        try {
            await lock.release();
            log("debug", "屏幕锁已释放");
        } catch (_) {
            // 已被浏览器释放（切后台/锁屏），忽略
        }
    }

    async function acquire() {
        if (!enabled || !isSupported()) return;
        if (document.hidden) return;
        // 已持有时不重新申请：释放再获取的间隙里屏幕有机会熄灭，且没有实际收益
        if (sentinel) return;
        try {
            const lock = await navigator.wakeLock.request("screen");
            sentinel = lock;
            lock.addEventListener(
                "release",
                () => {
                    // 只认当前这把锁：旧锁的释放事件可能晚于新锁赋值才送达，直接把 sentinel 置空会误伤新锁
                    if (sentinel !== lock) return;
                    sentinel = null;
                    log("debug", "屏幕锁已被系统释放（切后台/锁屏）");
                },
                { once: true },
            );
            log("debug", "屏幕锁已获取");
            errorHint = "";
        } catch (err) {
            sentinel = null;
            const reason = err && err.name ? err.name : String(err);
            log("warn", `获取屏幕锁失败: ${reason}`);
            // 申请被拒时回退为关：否则按钮显示开、实际没有锁，用户会误以为屏幕不会熄灭，
            // 而且持久化的“开”会让下次打开页面继续重试同样的失败
            if (enabled) {
                enabled = false;
                errorHint = reason;
                try {
                    localStorage.setItem(STORAGE_KEY, "0");
                } catch (_) {
                    // localStorage 不可用时只丢持久化，回退本身已经生效
                }
            }
            render();
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
        // 申请失败回退后按钮不能只显示“关”，否则用户不知道刚才是申请失败而不是自己关掉了
        const failed = !enabled && errorHint !== "";
        btn.textContent = failed ? "防息屏：不可用" : `防息屏：${enabled ? "开" : "关"}`;
        // 开关状态只用两个字表达，扫一眼很难判断，这里用底色区分：开启为绿色，关闭为半透明黑
        btn.style.background = enabled ? "rgba(32,160,90,.9)" : "rgba(0,0,0,.55)";
        btn.style.borderColor = enabled ? "rgba(32,160,90,.9)" : "rgba(255,255,255,.35)";
        btn.setAttribute("aria-pressed", enabled ? "true" : "false");
        if (failed) {
            btn.title = `无法保持屏幕常亮（${errorHint}），已自动关闭，再点一次可以重试`;
        } else {
            btn.title = enabled ? "屏幕将保持常亮（切后台自动释放）" : "开启后屏幕不会自动熄灭";
        }
    }

    function toggle() {
        enabled = !enabled;
        // 每次点击都算一次新的意图，先清掉上次的失败提示，失败会重新写入
        errorHint = "";
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