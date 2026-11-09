// ==UserScript==
// @name         哔哩哔哩网页全屏快捷键
// @namespace    local.eggroll.bilibili
// @version      0.1.0
// @description  在哔哩哔哩视频页与直播页按 V 切换网页全屏，行为与播放器上的“网页全屏”按钮一致
// @match        https://www.bilibili.com/*
// @match        https://live.bilibili.com/*
// @noframes
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * 为什么这样设计：
 * - B 站自带 f 真全屏，但网页全屏没有快捷键；这里只补一个与 f 同层级的页面级快捷键。
 * - 站内 w 是投币、g 是关注，都不能直接占用；v 不在视频播放器的快捷键表里，作为单键更省事。
 * - 不缓存按钮或播放器节点：B 站是单页应用，换 P、切集、站内跳转都会重建播放器，
 *   每次按键时重新查询，才不会点到已经脱离文档的旧节点。
 * - 状态只看容器上的 data-screen：进入网页全屏后按钮的 aria-label 仍是“网页全屏”，
 *   变化的只是 tooltip，不能拿 aria-label 判断当前是否已经全屏。
 * - 优先点播放器自己的按钮，让它的内部逻辑决定退出时回到哪个状态；只有按钮不存在
 *   （部分页面会按 controlsList 不渲染该按钮）时才退回播放器接口。
 * - 直播页是另一套播放器，没有 bpx 控件，只能走 livePlayer 接口；状态标记也不同，
 *   以 body.player-full-win 为准，不能套用视频页的 data-screen。
 * - @grant none：只使用标准 DOM 与页面已有对象，不需要 GM_* 能力。
 */
(function () {
    "use strict";

    // 同一页面被重复注入时，第二个监听会把第一个的切换再切回去，净效果为零
    const INSTALL_FLAG = Symbol.for("local.eggroll.bilibili.webfullscreen");
    if (window[INSTALL_FLAG]) return;
    window[INSTALL_FLAG] = true;

    const SHORTCUT_CODE = "KeyV";
    const LOG_PREFIX = "[网页全屏]";
    const IS_LIVE_PAGE = location.hostname === "live.bilibili.com";

    // 这些 input 类型不接收文本输入，聚焦时仍应允许快捷键，与站内 f 的判定保持一致
    const NON_TEXT_INPUT_TYPES = new Set([
        "button",
        "checkbox",
        "color",
        "file",
        "hidden",
        "image",
        "radio",
        "range",
        "reset",
        "submit",
    ]);

    function isTextEntry(element) {
        if (!(element instanceof Element)) return false;
        if (element.isContentEditable) return true;
        if (
            element.matches(
                "textarea, select, [role='textbox'], [role='searchbox'], [role='combobox']",
            )
        ) {
            return true;
        }
        if (element.matches("input")) {
            const type = (element.getAttribute("type") || "text").toLowerCase();
            return !NON_TEXT_INPUT_TYPES.has(type);
        }
        return false;
    }

    function isEditing(event) {
        // 焦点可能落在 Shadow DOM 内，composedPath 能拿到真实的输入目标
        const path =
            typeof event.composedPath === "function" ? event.composedPath() : [];
        for (const node of path) {
            if (node instanceof Element && isTextEntry(node)) return true;
        }
        return isTextEntry(event.target) || isTextEntry(document.activeElement);
    }

    function findPlayer() {
        // 信息流里可能有悬停预览播放器，取当前可见面积最大的那个容器
        let best = null;
        for (const container of document.querySelectorAll(
            ".bpx-player-container[data-screen]",
        )) {
            const rect = container.getBoundingClientRect();
            const area = rect.width * rect.height;
            if (area <= 0) continue;
            if (!best || area > best.area) best = { container, area };
        }
        if (!best) return null;
        return {
            container: best.container,
            button: best.container.querySelector(".bpx-player-ctrl-web"),
        };
    }

    function callPlayerApi(container) {
        const player = window.player;
        const screenKind = window.nano && window.nano.ScreenKind;
        if (!player || typeof player.requestStatue !== "function") return false;
        if (
            !screenKind ||
            typeof screenKind.Web !== "number" ||
            typeof screenKind.Normal !== "number"
        ) {
            return false;
        }

        const entering = container.getAttribute("data-screen") !== "web";
        try {
            player.requestStatue(entering ? screenKind.Web : screenKind.Normal);
            return true;
        } catch (err) {
            console.debug(`${LOG_PREFIX} 播放器接口调用失败`, err);
            return false;
        }
    }

    function toggleLivePlayer() {
        const player = window.livePlayer;
        if (!player || typeof player.setFullscreenStatus !== "function") return false;

        const entered = document.body.classList.contains("player-full-win");
        try {
            player.setFullscreenStatus(entered ? 0 : 1);
            return true;
        } catch (err) {
            console.debug(`${LOG_PREFIX} 直播播放器接口调用失败`, err);
            return false;
        }
    }

    function toggle() {
        if (IS_LIVE_PAGE) {
            if (!toggleLivePlayer()) return false;
            console.debug(`${LOG_PREFIX} 已切换直播网页全屏`);
            return true;
        }

        const player = findPlayer();
        if (!player) return false;

        const entering = player.container.getAttribute("data-screen") !== "web";
        if (player.button) {
            player.button.click();
        } else if (!callPlayerApi(player.container)) {
            console.debug(`${LOG_PREFIX} 当前页面没有可用的网页全屏按钮或播放器接口`);
            return false;
        }

        console.debug(`${LOG_PREFIX} ${entering ? "进入" : "退出"}网页全屏`);
        return true;
    }

    function onKeyDown(event) {
        if (event.defaultPrevented) return;
        if (event.code !== SHORTCUT_CODE) return;
        if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
        if (event.repeat || event.isComposing || event.keyCode === 229) return;
        if (isEditing(event)) return;
        if (!toggle()) return;
        event.preventDefault();
    }

    window.addEventListener("keydown", onKeyDown);
})();