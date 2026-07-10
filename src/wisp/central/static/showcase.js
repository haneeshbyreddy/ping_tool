/*
 * showcase.js — public landing-page marketing overlay.
 *
 * The landing page (`/`) is an opaque pre-bundled artifact that rebuilds its
 * entire DOM once, on load, via `document.documentElement.replaceWith(...)`.
 * That wipes anything we place in the initial markup, so this script mounts two
 * fixed overlays that FLOAT on top of the bundle and re-mounts them if the swap
 * removes them. Data (`window.__WISP_SHOWCASE__`) is injected server-side from
 * the live DB; the offer copy lives here so it's trivial to tweak.
 *
 * Design: on-brand for the WISP dark/near-black canvas with the gold (#5680bd)
 * accent — restrained SaaS chrome, not a blinking marquee. Top bar = the
 * limited-time offer (urgency); bottom bar = social proof (trust).
 */
(function () {
  "use strict";
  try {
    var GOLD = "#5680bd";
    var CANVAS = "#09090b";
    var STYLE_ID = "wisp-showcase-style";
    var OFFER_ID = "wisp-offer";
    var TRUST_ID = "wisp-trust";
    var H_OFFER = 44;
    var H_TRUST = 46;

    var data = window.__WISP_SHOWCASE__ || {};
    if (!data.enabled) return;

    var names = Array.isArray(data.names) ? data.names.filter(Boolean) : [];
    var count = typeof data.count === "number" ? data.count : names.length;
    var reduce =
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // ---- Offer copy (edit freely) ----------------------------------------
    var OFFER = {
      lead: "Early access",
      body: "free today. Founding ISPs lock in ",
      highlight: "20% off",
      tail: " future pricing.",
      ctaLabel: "Claim your spot",
      ctaHref: "/app",
    };

    function el(tag, css, text) {
      var n = document.createElement(tag);
      if (css) n.style.cssText = css;
      if (text != null) n.textContent = text;
      return n;
    }

    function ensureStyle() {
      if (document.getElementById(STYLE_ID)) return;
      var s = document.createElement("style");
      s.id = STYLE_ID;
      s.textContent =
        "@keyframes wisp-marquee{from{transform:translateX(0)}" +
        "to{transform:translateX(-50%)}}" +
        "@keyframes wisp-pulse{0%,100%{opacity:1}50%{opacity:.35}}" +
        "#" + TRUST_ID + " .wisp-track{animation:wisp-marquee var(--wisp-dur,40s) linear infinite}" +
        "#" + TRUST_ID + ":hover .wisp-track{animation-play-state:paused}" +
        "#" + OFFER_ID + " a.wisp-cta:hover{background:rgba(86,128,189,.16)}" +
        "@media (max-width:640px){#" + OFFER_ID + " .wisp-cta{display:none!important}}";
      (document.head || document.documentElement).appendChild(s);
    }

    // ---- Top offer bar ---------------------------------------------------
    function buildOffer() {
      var bar = el(
        "div",
        "position:fixed;top:0;left:0;right:0;height:" + H_OFFER + "px;" +
          "z-index:2147483000;display:flex;align-items:center;justify-content:center;" +
          "gap:14px;padding:0 16px;font:500 14px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;" +
          "color:#e5e5ea;background:linear-gradient(180deg,#101014,#0e0e11);" +
          "border-bottom:1px solid rgba(86,128,189,.28);" +
          "box-shadow:0 1px 0 rgba(0,0,0,.4);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)"
      );
      bar.id = OFFER_ID;

      var dot = el(
        "span",
        "flex:0 0 auto;width:7px;height:7px;border-radius:50%;background:" +
          GOLD + ";box-shadow:0 0 8px " + GOLD + ";animation:wisp-pulse 2.4s ease-in-out infinite"
      );

      var msg = el("span", "text-align:center;letter-spacing:.1px");
      msg.appendChild(el("strong", "color:" + GOLD + ";font-weight:700", OFFER.lead));
      msg.appendChild(document.createTextNode(" " + OFFER.body));
      msg.appendChild(el("strong", "color:" + GOLD + ";font-weight:700", OFFER.highlight));
      msg.appendChild(document.createTextNode(OFFER.tail));

      var cta = el(
        "a",
        "flex:0 0 auto;text-decoration:none;color:" + GOLD + ";font-weight:600;" +
          "border:1px solid rgba(86,128,189,.4);border-radius:999px;padding:5px 13px;" +
          "font-size:13px;transition:background .15s ease;white-space:nowrap"
      );
      cta.className = "wisp-cta";
      cta.href = OFFER.ctaHref;
      cta.textContent = OFFER.ctaLabel + " →";

      bar.appendChild(dot);
      bar.appendChild(msg);
      bar.appendChild(cta);
      return bar;
    }

    // ---- Bottom social-proof ticker --------------------------------------
    function nodeGlyph() {
      var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("width", "16");
      svg.setAttribute("height", "16");
      svg.style.cssText = "flex:0 0 auto;opacity:.9";
      svg.innerHTML =
        '<g fill="none" stroke="' + GOLD + '" stroke-width="1.5" stroke-linecap="round">' +
        '<circle cx="12" cy="12" r="2.2" fill="' + GOLD + '"/>' +
        '<circle cx="4" cy="6" r="1.6"/><circle cx="4" cy="18" r="1.6"/><circle cx="20" cy="12" r="1.6"/>' +
        '<path d="M6 6.6 10.2 11M6 17.4 10.2 13M14 12h4" stroke-opacity=".7"/></g>';
      return svg;
    }

    function buildTrust() {
      if (count < 1) return null;
      var bar = el(
        "div",
        "position:fixed;bottom:0;left:0;right:0;height:" + H_TRUST + "px;z-index:2147482999;" +
          "display:flex;align-items:center;background:" + CANVAS + ";" +
          "border-top:1px solid rgba(86,128,189,.16);" +
          "font:14px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#8e8e97"
      );
      bar.id = TRUST_ID;

      var label = el(
        "div",
        "flex:0 0 auto;display:flex;align-items:center;gap:9px;padding:0 18px;height:100%;" +
          "border-right:1px solid rgba(86,128,189,.14);color:" + GOLD + ";" +
          "font-weight:600;letter-spacing:.2px;white-space:nowrap"
      );
      label.className = "wisp-label";
      // Small-N reads weak as "Trusted by 1 ISP"; lead with the names instead.
      var labelText =
        count >= 3 ? "Trusted by " + count + " ISPs" : "Now live with";
      label.appendChild(document.createTextNode(labelText));

      bar.appendChild(label);

      if (!names.length) {
        // No opted-in names — the count alone is the proof.
        return bar;
      }

      var viewport = el(
        "div",
        "position:relative;flex:1 1 auto;overflow:hidden;height:100%;" +
          "-webkit-mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent);" +
          "mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent)"
      );

      function chipRow() {
        var row = el(
          "div",
          "flex:0 0 auto;display:flex;align-items:center;height:100%;padding-right:0"
        );
        names.forEach(function (nm) {
          var chip = el(
            "span",
            "display:inline-flex;align-items:center;gap:10px;padding:0 22px;" +
              "white-space:nowrap;color:#c6c6cd;font-weight:500"
          );
          chip.appendChild(document.createTextNode(nm));
          chip.appendChild(
            el("span", "color:" + GOLD + ";opacity:.55;font-size:16px", "·")
          );
          row.appendChild(chip);
        });
        return row;
      }

      var track = el(
        "div",
        "display:flex;align-items:center;height:100%;width:max-content"
      );
      track.className = "wisp-track";
      // Two identical halves so the -50% loop is seamless.
      track.appendChild(chipRow());
      track.appendChild(chipRow());
      // Duration scales with content so speed stays constant regardless of count.
      var dur = Math.max(18, names.length * 4);
      track.style.setProperty("--wisp-dur", dur + "s");
      if (reduce) track.style.animation = "none";

      viewport.appendChild(track);
      bar.appendChild(viewport);
      return bar;
    }

    // ---- Mount / self-heal ----------------------------------------------
    function applyPadding() {
      if (!document.body) return;
      var top = document.getElementById(OFFER_ID) ? H_OFFER : 0;
      var bot = document.getElementById(TRUST_ID) ? H_TRUST : 0;
      document.body.style.setProperty("padding-top", top + "px", "important");
      document.body.style.setProperty("padding-bottom", bot + "px", "important");
    }

    function mount() {
      if (!document.body) return;
      ensureStyle();
      // Offer bar is permanent — always shown, no dismiss.
      if (!document.getElementById(OFFER_ID)) {
        document.body.appendChild(buildOffer());
      }
      if (!document.getElementById(TRUST_ID)) {
        var t = buildTrust();
        if (t) document.body.appendChild(t);
      }
      applyPadding();
    }

    // The bundle swaps the whole documentElement once on load, detaching our
    // overlays. Watch the Document node (which never changes) and re-mount.
    var obs = new MutationObserver(function () {
      mount();
    });
    try {
      obs.observe(document, { childList: true });
    } catch (e) {}
    // Belt-and-suspenders across the bundle's async render timeline.
    [0, 150, 600, 1500, 3000].forEach(function (ms) {
      setTimeout(mount, ms);
    });
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", mount);
    }
    window.addEventListener("load", mount);
  } catch (err) {
    // Never let marketing chrome break the landing page.
    if (window.console) console.warn("showcase overlay failed:", err);
  }
})();
