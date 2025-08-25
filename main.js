// static/main.js
// OneTouch Portal - Frontend Bootstrap 🍀
// ---------------------------------------------------------
// 特色：
// - ✅ 側邊欄開闔 (localStorage 記憶)
// - ✅ Toast 通知 (success/info/warn/error)
// - ✅ 全域 fetch 工具 (JSON 送受、錯誤統一處理)
// - ✅ 載入中遮罩、按鈕 loading 狀態
// - ✅ 表單小驗證 + AJAX 送出 (class="js-ajax-form")
// - ✅ 事件委派（data-action）
// - ✅ 按鈕動畫（ripple + 凹陷 active）
// - ✅ 去抖/節流、小工具集
// - ✅ 心跳檢查 /api/ping（可自行關閉）
//
// 放著就能跑；有元素就作用，沒有也不報錯。
// ---------------------------------------------------------

(() => {
  "use strict";

  // -----------------------------
  // 小工具 & 選擇器
  // -----------------------------
  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
  const on = (el, evt, selOrHandler, handler) => {
    // 支援事件委派：on(document,'click','[data-action="xx"]',fn)
    if (typeof selOrHandler === "function") {
      el.addEventListener(evt, selOrHandler);
    } else {
      el.addEventListener(evt, (e) => {
        const target = e.target.closest(selOrHandler);
        if (target) handler(e, target);
      });
    }
  };
  const debounce = (fn, wait = 300) => {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), wait);
    };
  };
  const throttle = (fn, wait = 300) => {
    let last = 0;
    return (...args) => {
      const now = Date.now();
      if (now - last >= wait) {
        last = now;
        fn(...args);
      }
    };
  };

  // -----------------------------
  // 動態注入基礎樣式（Toast、遮罩、按鈕active）
  // -----------------------------
  const injectBaseStyles = () => {
    if ($("#__otp_base_styles")) return;
    const css = `
      .otp-toast-wrap{position:fixed;right:16px;top:16px;z-index:9999;display:flex;flex-direction:column;gap:8px}
      .otp-toast{min-width:220px;max-width:420px;background:#111827;color:#fff;padding:10px 12px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.2);font-size:14px;display:flex;align-items:center;gap:8px;opacity:.98}
      .otp-toast.success{background:#16a34a}.otp-toast.info{background:#2563eb}.otp-toast.warn{background:#f59e0b}.otp-toast.error{background:#dc2626}
      .otp-toast .msg{flex:1}
      .otp-mask{position:fixed;inset:0;background:rgba(0,0,0,.25);display:none;z-index:9998}
      .otp-mask.show{display:block}
      .otp-spinner{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);border:4px solid rgba(255,255,255,.3);border-top:4px solid #fff;border-radius:50%;width:40px;height:40px;animation:otpSpin 1s linear infinite}
      @keyframes otpSpin{to{transform:translate(-50%,-50%) rotate(360deg)}}
      /* 按鈕動畫：ripple + active 凹陷 */
      .btn,.button,button,[data-btn]{position:relative;overflow:hidden;transition:transform .05s ease}
      .btn:active,.button:active,button:active,[data-btn]:active{transform:translateY(1px) scale(0.98);box-shadow:inset 2px 2px 6px rgba(0,0,0,.15)}
      .ripple{position:absolute;border-radius:50%;transform:scale(0);animation:ripple .45s linear;background:rgba(255,255,255,.5)}
      @keyframes ripple{to{transform:scale(4);opacity:0}}
      /* 側邊欄收合狀態（請配合你的 CSS） */
      .sidebar.collapsed{width:64px}
    `.trim();
    const style = document.createElement("style");
    style.id = "__otp_base_styles";
    style.textContent = css;
    document.head.appendChild(style);

    // 遮罩容器
    if (!$("#__otp_mask")) {
      const mask = document.createElement("div");
      mask.id = "__otp_mask";
      mask.className = "otp-mask";
      mask.innerHTML = `<div class="otp-spinner" role="status" aria-label="loading"></div>`;
      document.body.appendChild(mask);
    }
    // Toast 容器
    if (!$("#__otp_toast")) {
      const wrap = document.createElement("div");
      wrap.id = "__otp_toast";
      wrap.className = "otp-toast-wrap";
      document.body.appendChild(wrap);
    }
  };

  // -----------------------------
  // Toast 訊息 🍬
  // -----------------------------
  const toast = (message, type = "info", ms = 2600) => {
    const wrap = $("#__otp_toast");
    if (!wrap) return console.log(`[toast:${type}]`, message);
    const el = document.createElement("div");
    el.className = `otp-toast ${type}`;
    el.innerHTML = `<span class="icon">🔔</span><div class="msg">${message}</div>`;
    wrap.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 300);
    }, ms);
  };
  toast.success = (m, t) => toast(m, "success", t);
  toast.info = (m, t) => toast(m, "info", t);
  toast.warn = (m, t) => toast(m, "warn", t);
  toast.error = (m, t) => toast(m, "error", t);

  // -----------------------------
  // 載入中遮罩 & 按鈕 loading
  // -----------------------------
  const mask = {
    show() { $("#__otp_mask")?.classList.add("show"); },
    hide() { $("#__otp_mask")?.classList.remove("show"); }
  };
  const btnLoading = {
    start(btn, text = "處理中…") {
      if (!btn) return;
      btn.dataset._oldHtml = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `⏳ ${text}`;
    },
    stop(btn) {
      if (!btn) return;
      btn.disabled = false;
      if (btn.dataset._oldHtml) btn.innerHTML = btn.dataset._oldHtml;
      delete btn.dataset._oldHtml;
    }
  };

  // -----------------------------
  // 取得 CSRF 或 JWT（視專案而定）
  // -----------------------------
  const getToken = () => {
    // 你可以在 <meta name="csrf-token" content="{{ csrf }}"> 注入
    const meta = $('meta[name="csrf-token"]');
    return meta?.content || "";
  };

  // -----------------------------
  // 全域 fetch 工具（JSON）
  // -----------------------------
  const fetchJSON = async (url, opts = {}) => {
    const headers = Object.assign(
      { "Accept": "application/json" },
      opts.headers || {}
    );
    const res = await fetch(url, { ...opts, headers });
    let data = null;
    const ct = res.headers.get("Content-Type") || "";
    if (ct.includes("application/json")) {
      data = await res.json().catch(() => null);
    } else {
      data = await res.text().catch(() => null);
    }
    if (!res.ok) {
      const msg = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return data;
  };

  const submitJSON = async (url, method = "POST", payload = {}, opts = {}) => {
    const token = getToken();
    const headers = Object.assign(
      {
        "Content-Type": "application/json",
        "Accept": "application/json",
        ...(token ? { "X-CSRF-Token": token } : {})
      },
      opts.headers || {}
    );
    return fetchJSON(url, { method, headers, body: JSON.stringify(payload) });
  };

  // -----------------------------
  // Ripple + active 效果
  // -----------------------------
  const attachButtonEffects = () => {
    on(document, "click", ".btn, .button, button, [data-btn]", (e, target) => {
      const rect = target.getBoundingClientRect();
      const circle = document.createElement("span");
      const d = Math.max(rect.width, rect.height);
      circle.style.width = circle.style.height = `${d}px`;
      circle.style.left = `${e.clientX - rect.left - d / 2}px`;
      circle.style.top = `${e.clientY - rect.top - d / 2}px`;
      circle.className = "ripple";
      target.appendChild(circle);
      setTimeout(() => circle.remove(), 450);
    });
  };

  // -----------------------------
  // 側邊欄收合 + 記憶
  // -----------------------------
  const initSidebarToggle = () => {
    const sidebar = $(".sidebar");
    const toggleBtn = $("#menu-toggle");
    if (!sidebar || !toggleBtn) return;

    // 初始狀態
    const saved = localStorage.getItem("otp.sidebar.collapsed");
    if (saved === "1") sidebar.classList.add("collapsed");

    toggleBtn.addEventListener("click", () => {
      sidebar.classList.toggle("collapsed");
      const collapsed = sidebar.classList.contains("collapsed") ? "1" : "0";
      localStorage.setItem("otp.sidebar.collapsed", collapsed);
    });
  };

  // -----------------------------
  // 表單小驗證 + AJAX 送出
  // HTML：<form class="js-ajax-form" action="/api/xxx" method="post">
  // -----------------------------
  const initAjaxForms = () => {
    on(document, "submit", "form.js-ajax-form", async (e, form) => {
      e.preventDefault();
      const action = form.getAttribute("action") || form.dataset.api;
      if (!action) return toast.warn("找不到送出位址（action）");

      // 簡易必填驗證
      const requireds = $$("[required]", form);
      for (const el of requireds) {
        if (!String(el.value || "").trim()) {
          el.focus();
          return toast.warn(`請完整填寫：${el.name || el.id || "欄位"}`);
        }
      }

      const btn = $("button[type=submit], [data-submit]", form);
      try {
        btnLoading.start(btn);
        mask.show();

        let payload;
        if (form.enctype === "multipart/form-data") {
          payload = new FormData(form);
          const res = await fetch(action, { method: form.method || "POST", body: payload });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const data = await res.json().catch(() => ({}));
          toast.success("已送出");
          form.dispatchEvent(new CustomEvent("ajax:done", { detail: data }));
        } else {
          const fd = new FormData(form);
          payload = Object.fromEntries(fd.entries());
          const data = await submitJSON(action, form.method || "POST", payload);
          toast.success("已送出");
          form.dispatchEvent(new CustomEvent("ajax:done", { detail: data }));
        }
      } catch (err) {
        console.error(err);
        toast.error(`送出失敗：${err.message || err}`);
        form.dispatchEvent(new CustomEvent("ajax:error", { detail: err }));
      } finally {
        mask.hide();
        btnLoading.stop(btn);
      }
    });
  };

  // -----------------------------
  // 通用事件委派（data-action）
  // 例：<button data-action="reindex">一鍵建立索引</button>
  // -----------------------------
  const initActions = () => {
    on(document, "click", "[data-action]", async (e, el) => {
      const act = el.dataset.action;
      if (!act) return;

      try {
        // 可依需求擴充
        if (act === "reindex") {
          btnLoading.start(el, "建立索引…");
          mask.show();
          const data = await submitJSON("/api/reindex", "POST", {});
          toast.success(`索引完成 ✅ 共 ${data?.count ?? "N"} 筆`);
        }

        if (act === "logout") {
          const ok = confirm("確定要登出嗎？");
          if (!ok) return;
          await fetchJSON("/logout", { method: "POST" });
          location.href = "/login";
        }

        if (act === "copy") {
          const sel = el.dataset.target;
          const t = sel ? $(sel) : null;
          const txt = t?.value || t?.innerText || el.dataset.text || "";
          await navigator.clipboard.writeText(txt);
          toast.success("已複製到剪貼簿");
        }
      } catch (err) {
        console.error(err);
        toast.error(`操作失敗：${err.message || err}`);
      } finally {
        btnLoading.stop(el);
        mask.hide();
      }
    });
  };

  // -----------------------------
  // 心跳檢查（可關閉）
  // -----------------------------
  const startHeartbeat = () => {
    const ENABLE = true; // 想關掉改成 false
    if (!ENABLE) return;
    const ping = async () => {
      try {
        await fetchJSON("/api/ping", { cache: "no-store" });
      } catch {
        toast.warn("與伺服器連線異常，請稍後重試");
      }
    };
    setInterval(ping, 60_000); // 每 60 秒 ping 一次
  };

  // -----------------------------
  // 初始化
  // -----------------------------
  document.addEventListener("DOMContentLoaded", () => {
    injectBaseStyles();
    attachButtonEffects();
    initSidebarToggle();
    initAjaxForms();
    initActions();
    startHeartbeat();
    console.log("✅ OneTouch Portal main.js loaded");
  });

  // -----------------------------
  // 將常用工具掛在 window（選擇性）
  // -----------------------------
  window.OTP = {
    $, $$, on, toast, mask, fetchJSON, submitJSON, debounce, throttle
  };
})();
