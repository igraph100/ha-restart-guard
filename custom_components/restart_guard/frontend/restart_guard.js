/*
 * restart_guard.js  --  bundled with the Restart Guard integration.
 *
 * Injects a warning into Home Assistant's own "Restart Home Assistant" dialog
 * (3-dot menu) when a timed automation is about to run, and makes the Restart
 * row need a second, deliberate tap.
 *
 * It also guards the restarts that never go near that dialog - the HACS
 * "Restart Home Assistant" button, a restart-required repair, a core/OS/
 * Supervisor update that reboots when it finishes, a host reboot, a dashboard
 * button, Developer Tools - by wrapping the three calls every one of them ends
 * up making, and asking for confirmation there instead. See "guarding the
 * rest" below.
 *
 * The integration registers this file automatically. Nothing to install by
 * hand, no /config/www copy, no extra_module_url entry.
 *
 * Note on the design: when you close the restart dialog, Home Assistant tears
 * down the dialog's rendered content but leaves the empty <dialog-restart>
 * host element in the DOM. Reopening rebuilds the content without ever adding
 * a new node to <home-assistant>, so a childList observer on the app root
 * fires exactly once, on the very first open. Decorating from that observer
 * alone means the banner never comes back after the first close.
 *
 * So: watch the host's own shadow root for the content being rebuilt (instant
 * response), and also poll, so the countdown stays live while the dialog sits
 * open. `.content` only exists while the dialog is open, which makes it a
 * reliable open/closed signal.
 *
 * Verified against Home Assistant 2026.7.
 */
(() => {
  "use strict";

  const SHOW_WHEN_CLEAR = true; // green "safe to restart" note when nothing is due
  // brief note when a restart-class action is waved through without asking.
  // "no prompt" otherwise means both "the guard checked and it is fine" and
  // "the guard never ran", which is impossible to tell apart on a phone.
  const SHOW_ALLOWED_NOTICE = true;
  const DIALOG_TAG = "dialog-restart";
  const CLASS = "restart-guard-banner";
  const WRAP_CLASS = "restart-guard-wrap";
  const FALLBACK_WINDOW = 6;
  const TICK_MS = 750;
  const MAX_LISTED = 6;

  const root = () => document.querySelector("home-assistant");

  // ---------------------------------------------------------------- data
  function guardState() {
    const hass = root() && root().hass;
    if (!hass) return null;
    const direct = hass.states["sensor.restart_guard"];
    if (direct && direct.attributes && direct.attributes.restart_guard) {
      return direct;
    }
    // survives the entity being renamed, or landing on _2
    for (const id in hass.states) {
      const candidate = hass.states[id];
      if (candidate.attributes && candidate.attributes.restart_guard === true) {
        return candidate;
      }
    }
    return null;
  }

  function guardInfo() {
    const state = guardState();
    if (!state) return { missing: true };
    const attrs = state.attributes || {};
    const mins = parseFloat(state.state);
    const win = parseFloat(attrs.warn_window) || FALLBACK_WINDOW;
    const count = parseInt(attrs.count, 10) || 0;
    const items = attrs.items || [];
    return {
      entityId: state.entity_id,
      mins: isFinite(mins) ? mins : null,
      win: win,
      items: items,
      // split at the warn window: what a restart threatens, and what is
      // merely coming up later in the lookahead
      atRisk: items.filter((item) => Number(item.minutes) <= win),
      later: items.filter((item) => Number(item.minutes) > win),
      count: count,
      lookahead: parseInt(attrs.lookahead, 10) || 60,
      error: attrs.error || null,
      // runs in progress right now: a restart cuts them off mid-way
      running: attrs.running || [],
      blocking: (attrs.running || []).filter((run) => !run.parked),
      parked: (attrs.running || []).filter((run) => run.parked),
      dueSoon: count > 0 && isFinite(mins) && mins <= win,
      clear: count > 0 && isFinite(mins) && mins > win,
      // Anything part-way through a run is at risk, however long it has been
      // going. A three-hour delay is exactly the case a restart destroys, and
      // it used to read as safe simply for taking a while.
      get warn() {
        return this.dueSoon || this.running.length > 0;
      },
    };
  }

  function forceRefresh(entityId) {
    const hass = root() && root().hass;
    if (!hass || !entityId) return;
    hass
      .callService("homeassistant", "update_entity", { entity_id: entityId })
      .catch(() => {});
  }

  // ---------------------------------------------------------------- view
  /*
   * One rule set for the item rows, shared by the dialog banner and the confirm
   * card so both look identical. Each item is its own row with a bar down the
   * left: the name on top in bold, the time and countdown beneath it, because
   * those are the two things worth reading at a glance.
   *
   * The tints are grey-on-alpha rather than a fixed colour so they sit
   * correctly on both the light and the dark alert backgrounds.
   */
  const ROW_CSS = `
      .rg-row {
        display: block;
        border-left: 3px solid currentColor;
        border-radius: 0 6px 6px 0;
        background: rgba(127, 127, 127, 0.14);
        padding: 5px 8px 5px 9px;
        margin: 5px 0;
      }
      .rg-nm {
        display: block; font-weight: 700; font-size: 14.5px; line-height: 1.35;
      }
      .rg-tm { display: block; font-size: 13px; opacity: 0.8; }
      /*
       * A run already in progress. Everything else on the banner is a
       * prediction; this one is happening, and restarting ends it. So it gets
       * a heavier bar, a stronger fill and full-strength text rather than the
       * dimmed meta line the upcoming rows use.
       */
      .rg-row.rg-live {
        border-left-width: 5px;
        background: rgba(219, 68, 55, 0.18);
      }
      .rg-row.rg-live .rg-tm { opacity: 1; font-weight: 700; }
      .rg-row.rg-live .rg-pill { background: rgba(219, 68, 55, 0.3); }
      /* which kind of thing this row is: automation, script or Scheduler */
      .rg-pill {
        display: inline-block;
        margin-right: 6px;
        padding: 0 7px;
        border-radius: 999px;
        background: rgba(127, 127, 127, 0.22);
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.02em;
        line-height: 17px;
        vertical-align: 1px;
        opacity: 0.95;
        white-space: nowrap;
      }
      /* the second-tap prompt: a filled bar, so it is obvious the state changed */
      .rg-armed {
        display: block; margin-top: 10px; padding: 7px 10px;
        font-weight: 700; text-align: center; border-radius: 6px;
        background: rgba(219, 68, 55, 0.16);
      }
  `;

  /** Only used when ha-alert is unavailable; ha-alert brings its own styling. */
  function styleTag() {
    const style = document.createElement("style");
    style.className = CLASS + "-style";
    style.textContent = `
      .${WRAP_CLASS} { margin: 0 16px 8px; }
      .${WRAP_CLASS}[hidden] { display: none; }
      .${CLASS} {
        padding: 12px 16px;
        border-radius: 12px;
        font-size: 14px;
        line-height: 1.5;
        border: 1px solid transparent;
      }
      .${CLASS}.warn {
        background: rgba(219, 68, 55, 0.12);
        border-color: rgba(219, 68, 55, 0.6);
        color: var(--primary-text-color);
      }
      .${CLASS}.clear {
        background: rgba(67, 160, 71, 0.12);
        border-color: rgba(67, 160, 71, 0.45);
        color: var(--primary-text-color);
      }
      .${CLASS} .rg-title { font-weight: 600; display: block; margin-bottom: 4px; }
      .rg-line { display: block; }
      .rg-hint { display: block; margin-top: 6px; opacity: 0.75; }
      ${ROW_CSS}
    `;
    return style;
  }

  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"]/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
    }[ch]));
  }

  /** Clock time in the user's own 12/24-hour preference. */
  function clockTime(item) {
    const hass = root() && root().hass;
    const stamp = item && item.at_ts;
    if (!hass || !stamp) return (item && item.when) || "";
    const locale = hass.locale || {};
    const opts = { hour: "numeric", minute: "2-digit" };
    const lang = locale.language || undefined;
    const when = new Date(stamp * 1000);
    try {
      if (locale.time_format === "12") {
        return when.toLocaleTimeString(lang, { ...opts, hour12: true });
      }
      if (locale.time_format === "24") {
        return when.toLocaleTimeString(lang, { ...opts, hour12: false });
      }
      if (locale.time_format === "system") {
        return when.toLocaleTimeString(undefined, opts);
      }
      return when.toLocaleTimeString(lang, opts); // "language" / unset
    } catch (err) {
      return item.when || "";
    }
  }

  function relative(minutes) {
    const mins = Number(minutes);
    if (!isFinite(mins)) return "";
    if (mins < 1) return "in under a minute";
    if (mins < 60) return `in ${Math.round(mins)} min`;
    const hours = Math.floor(mins / 60);
    const rest = Math.round(mins % 60);
    if (rest === 0) return `in ${hours} hr`;
    return `in ${hours} hr ${rest} min`;
  }

  /**
   * One item as its own row: name on top, time and countdown beneath, with a
   * coloured bar down the left. Names and times are what you actually need to
   * read, so they get their own lines rather than being buried in a sentence.
   */
  function row(name, detail, pill, extra) {
    return (
      `<span class="rg-row${extra ? " " + extra : ""}">` +
      `<span class="rg-nm">${esc(name)}</span>` +
      `<span class="rg-tm">` +
      (pill ? `<span class="rg-pill">${esc(pill)}</span>` : "") +
      `${esc(detail)}</span></span>`
    );
  }

  /**
   * Where the run came from. Worth saying on every row rather than only in the
   * title: a warning can list an automation and a schedule together, and what
   * you do about it differs depending on which one it is.
   */
  function kindOf(item) {
    if (item && item.source === "schedule") return "Scheduler";
    return String((item && item.entity_id) || "").startsWith("script.")
      ? "Script"
      : "Automation";
  }

  /** The pill already says "Scheduler", so drop a redundant name prefix. */
  function trimKind(name) {
    return String(name == null ? "" : name).replace(/^Schedule:\s*/i, "");
  }

  function describe(item) {
    return row(
      trimKind(item.alias),
      `${clockTime(item)} · ${relative(item.minutes)}`,
      kindOf(item)
    );
  }

  /** "for 12 s" / "for 4 min" / "for 3 hr 20 min" - how long it has been going. */
  function ago(seconds) {
    if (seconds == null || !isFinite(seconds)) return "";
    if (seconds < 60) return `for ${Math.round(seconds)} s`;
    const mins = Math.round(seconds / 60);
    if (mins < 60) return `for ${mins} min`;
    const hours = Math.floor(mins / 60);
    const rest = mins % 60;
    return rest ? `for ${hours} hr ${rest} min` : `for ${hours} hr`;
  }

  /**
   * A run in progress. Said plainly and marked out from the upcoming rows,
   * because this is the one case where the damage is certain rather than
   * predicted: the automation is running now, and restarting ends it here.
   * How long it has been going is context, not the point.
   */
  function describeRun(run) {
    const detail = ago(run.seconds_ago);
    return row(
      trimKind(run.alias),
      detail
        ? `Still running ${detail} — restarting stops it`
        : "Still running — restarting stops it",
      kindOf(run),
      "rg-live"
    );
  }

  function plural(count, one, many) {
    return count === 1 ? one : many.replace("{n}", String(count));
  }

  /** "in the next hour" / "in the next 25 min", for the lookahead window. */
  function horizon(minutes) {
    const mins = parseInt(minutes, 10);
    if (!isFinite(mins)) return "later";
    if (mins === 60) return "in the next hour";
    if (mins % 60 === 0) return `in the next ${mins / 60} hr`;
    return `in the next ${mins} min`;
  }


  /** Returns { type: "error"|"success", title, body } or null to hide. */
  function describeState(info, armed) {
    if (!info || info.missing) return null;

    if (info.warn) {
      // Every run in progress, first and together. A long-running one used to
      // be listed underneath the upcoming rows, which buried the one line on
      // screen describing damage that is certain rather than predicted.
      const runLines = info.running.slice(0, MAX_LISTED).map(describeRun).join("");
      // only what a restart would actually put at risk: the rest of the
      // lookahead is context, and reading it is what makes this slow
      const dueLines = info.dueSoon
        ? info.atRisk.slice(0, MAX_LISTED).map(describe).join("")
        : "";
      const hidden = info.dueSoon ? info.atRisk.length - MAX_LISTED : 0;
      const more =
        (hidden > 0
          ? `<span class="rg-line">…and ${hidden} more inside the window</span>`
          : "") +
        (info.dueSoon && info.later.length
          ? `<span class="rg-line">+${info.later.length} more ${horizon(
              info.lookahead
            )}</span>`
          : "");

      // lead with the number that decides it: how long you have
      const midRun = info.running.length;
      let title;
      if (midRun && info.dueSoon) {
        title = `Wait — something is running, next run ${relative(info.mins)}`;
      } else if (midRun) {
        title = plural(
          midRun,
          "Wait — an automation is running right now",
          "Wait — {n} automations are running right now"
        );
      } else {
        title = `Wait — next run ${relative(info.mins)}`;
      }

      const hints = [];
      if (midRun) {
        hints.push(
          "Restarting now cuts a run off part-way through, so any steps after a " +
            "wait or delay never happen."
        );
      }
      if (info.dueSoon) {
        hints.push(
          "A restart takes 30-90 seconds, and missed time triggers are not replayed."
        );
      }

      return {
        type: "error",
        title: title,
        body:
          runLines +
          dueLines +
          more +
          `<span class="rg-hint">${esc(hints.join(" "))}</span>` +
          (armed
            ? `<span class="rg-armed">Tap Restart again to go ahead anyway</span>`
            : ""),
      };
    }

    if (!SHOW_WHEN_CLEAR) return null;

    if (info.clear) {
      const next = info.items[0];
      // outside the warning window, so it is safe - but say what is coming and
      // when, rather than the blanket "nothing scheduled" that reads as a lie
      return {
        type: "success",
        title: next
          ? `Safe to restart — next run ${relative(next.minutes)}`
          : "Safe to restart",
        body: next ? describe(next) : "",
      };
    }

    return {
      type: "success",
      title: "Safe to restart",
      body:
        `<span class="rg-line">Nothing scheduled in the next ` +
        `${info.lookahead} minutes, and nothing is mid-run.</span>` +
        (info.error
          ? `<span class="rg-hint">Restart Guard reported: ${esc(info.error)}</span>`
          : ""),
    };
  }

  /** What the rendered banner will look like, used as the repaint key. */
  function signature(view) {
    return view ? [view.type, view.title, view.body].join("~") : "hidden";
  }

  function apply(parts, view) {
    if (!view) {
      parts.wrap.hidden = true;
      return;
    }
    parts.wrap.hidden = false;

    if (parts.alert) {
      parts.alert.setAttribute("alert-type", view.type);
      parts.alert.setAttribute("title", view.title);
      parts.alert.innerHTML = view.body;
      return;
    }
    // fallback styling when ha-alert is not registered
    parts.box.className = `${CLASS} ${view.type === "error" ? "warn" : "clear"}`;
    parts.box.innerHTML =
      `<span class="rg-title">${
        view.type === "error" ? "&#9888;&#65039; " : "&#10003; "
      }${esc(view.title)}</span>` + view.body;
  }

  // ------------------------------------------------------ guarding the rest
  /*
   * Everything that restarts Home Assistant from outside the restart dialog -
   * the HACS "Restart Home Assistant" button, a core update that installs and
   * then reboots on its own, a restart-required repair, a dashboard button,
   * Developer Tools - leaves the frontend as one of only three kinds of call:
   * a service call, a supervisor websocket message, or a REST call. Wrapping
   * those three guards every one of those paths at once, rather than trying to
   * decorate each dialog by hand and re-doing it every time one is redesigned.
   *
   * The bias is the same as the sensor's: anything we are not sure about goes
   * through untouched, and a missing or unavailable sensor never blocks.
   */
  const ACTIONS = {
    restart: {
      key: "restart",
      confirm: "Restart anyway",
      proceed: "Restart now",
      cancelled: "Restart cancelled by Restart Guard",
      note: "",
    },
    update: {
      key: "update",
      confirm: "Update anyway",
      proceed: "Update now",
      cancelled: "Update cancelled by Restart Guard",
      note:
        "A core update installs for several minutes and then restarts on its " +
        "own, so anything due while it works is missed too.",
    },
    reboot: {
      key: "reboot",
      confirm: "Reboot anyway",
      proceed: "Reboot now",
      cancelled: "Reboot cancelled by Restart Guard",
      note: "A host reboot takes noticeably longer than a Home Assistant restart.",
    },
    shutdown: {
      key: "shutdown",
      confirm: "Shut down anyway",
      proceed: "Shut down now",
      cancelled: "Shutdown cancelled by Restart Guard",
      note: "Nothing runs again until the machine is powered back on by hand.",
    },
  };

  /*
   * A confirmed action gets a short grace period so the same restart is not
   * questioned twice on its way down through two layers. It is scoped to the
   * KIND of action that was approved: a blanket window let an approved restart
   * wave a core update through behind it, which is how one slipped past.
   */
  const BYPASS_MS = 15000;
  // a silent pass-through only has to survive the hop to the next layer, which
  // takes about half a second - it should not linger like a real approval
  const HANDOFF_MS = 5000;
  let bypass = { until: 0, key: null };
  const allowOnce = (action, ms) => {
    bypass = {
      until: Date.now() + (ms || BYPASS_MS),
      key: (action && action.key) || "restart",
    };
  };
  const bypassed = (action) =>
    Date.now() < bypass.until && !!action && bypass.key === action.key;

  /*
   * Updates that restart something when they finish. The Operating System is
   * deliberately NOT one of them: installing it only stages the new image and
   * raises a "system reboot required" repair, leaving Home Assistant running.
   * That reboot is guarded in its own right, so warning at install time would
   * be warning about an action that cannot interrupt anything - and teaching
   * you to click through the prompt that does matter. Verified on HA OS 18.2:
   * the update entity went to 18.2 while Supervisor still reported 18.1.
   */
  const CORE_UPDATE_ID = /^update\.home_assistant_(core|supervisor)_update$/;
  const CORE_UPDATE_TITLES = ["home assistant core", "home assistant supervisor"];

  function asArray(value) {
    if (value == null) return [];
    return Array.isArray(value) ? value : [value];
  }

  function calledEntities(data, target) {
    const found = [];
    for (const source of [data, target]) {
      if (source && typeof source === "object") {
        for (const id of asArray(source.entity_id)) {
          if (typeof id === "string") found.push(id);
        }
      }
    }
    return found;
  }

  /** Core, OS and Supervisor updates restart on their own; add-ons do not. */
  function isCoreUpdate(entityId) {
    if (CORE_UPDATE_ID.test(entityId)) return true;
    const hass = root() && root().hass;
    const state = hass && hass.states[entityId];
    const title = state && state.attributes && state.attributes.title;
    return (
      !!title &&
      CORE_UPDATE_TITLES.indexOf(String(title).trim().toLowerCase()) !== -1
    );
  }

  function classifyService(domain, service, data, target) {
    const dom = String(domain || "").toLowerCase();
    const srv = String(service || "").toLowerCase();
    if (dom === "homeassistant" && srv === "restart") return ACTIONS.restart;
    if (dom === "hassio" && srv === "host_reboot") return ACTIONS.reboot;
    if (dom === "hassio" && srv === "host_shutdown") return ACTIONS.shutdown;
    if (dom === "update" && srv === "install") {
      return calledEntities(data, target).some(isCoreUpdate) ? ACTIONS.update : null;
    }
    return null;
  }

  /** Supervisor calls travel as websocket messages, not as service calls. */
  function classifyMessage(message) {
    if (!message || typeof message !== "object") return null;
    const type = String(message.type || "").toLowerCase();
    if (type === "call_service") {
      return classifyService(
        message.domain,
        message.service,
        message.service_data,
        message.target
      );
    }
    let path = "";
    if (type === "supervisor/api") {
      path = String(message.endpoint || "").toLowerCase();
    } else if (type.indexOf("hassio/") === 0) {
      path = "/" + type.slice("hassio/".length);
    } else {
      return null;
    }
    // match on the leading path, not the whole string: these endpoints take a
    // trailing version or sub-path often enough that an exact compare misses
    if (/^\/core\/restart(\/|$)/.test(path)) return ACTIONS.restart;
    if (/^\/host\/reboot(\/|$)/.test(path)) return ACTIONS.reboot;
    if (/^\/(host\/shutdown|core\/stop)(\/|$)/.test(path)) return ACTIONS.shutdown;
    // /os/update stages the image and reboots nothing - see CORE_UPDATE_ID
    if (/^\/(core|supervisor)\/update(\/|$)/.test(path)) return ACTIONS.update;
    return null;
  }

  /*
   * Repair flows are the odd one out: "Restart required" from HACS is fixed by
   * the *backend* calling homeassistant.restart, so there is no service call to
   * catch here. What there is, is the REST call that submits the fix - so
   * remember which flows came from a restart-flavoured issue and ask then.
   */
  const restartFlows = new Set();
  const RESTARTY = /restart|reboot/i;

  /*
   * What kind of issue this fix flow is for. Most repairs say it in the
   * issue_id ("restart_required_..."), but Supervisor-reported ones are
   * identified by a bare uuid - including "settings were changed which require
   * a system reboot" - so the id alone silently misses them. When the id says
   * nothing, ask the repairs list what the issue actually is.
   */
  async function issueIsRestarty(parameters) {
    const issue = parameters && (parameters.issue_id || parameters.issue);
    if (typeof issue === "string" && RESTARTY.test(issue)) return true;
    try {
      const hass = root() && root().hass;
      if (!hass || !hass.connection) return false;
      const listed = await hass.connection.sendMessagePromise({
        type: "repairs/list_issues",
      });
      const match = (listed.issues || []).find(
        (row) =>
          row.issue_id === issue &&
          (!parameters.handler || row.domain === parameters.handler)
      );
      if (!match) return false;
      return RESTARTY.test(
        [match.translation_key, match.issue_domain, match.issue_id]
          .filter(Boolean)
          .join(" ")
      );
    } catch (err) {
      return false; // never let a lookup failure block a repair
    }
  }

  const BANNER_CLASS = "rg-banner";
  // the banner currently on screen, if any: the notice checks this so the two
  // never say the same thing one after the other
  let liveBanner = null;

  /*
   * Home Assistant's own restart dialog carries the guard's verdict the moment
   * you open it. A restart-required repair did not - it only spoke up once you
   * pressed Submit, which is after the decision you wanted help with. This puts
   * the same read-only verdict in the repair dialog as it opens. No buttons:
   * it informs, and Submit still does the actual gating.
   */
  function showRepairBanner() {
    let tries = 0;
    const attach = () => {
      const host = openModalHost();
      if (!host) {
        if (++tries < 24) setTimeout(attach, 150); // dialog still rendering
        return;
      }
      if (host.querySelector("." + BANNER_CLASS)) return; // already announced
      // a prompt already took the floor in this dialog: nothing to add
      if (host.querySelector(".rg-card")) return;
      try {
        modalStyle();
        const card = document.createElement("div");
        const scoped = document.createElement("style");
        scoped.textContent = MODAL_CSS;
        const alert = customElements.get("ha-alert")
          ? document.createElement("ha-alert")
          : null;
        if (alert) alert.className = "rg-alert";
        const head = document.createElement("div");
        head.className = "rg-head";
        const body = document.createElement("div");
        body.className = "rg-body";
        card.append(scoped, ...(alert ? [alert] : [head, body]));

        let timer = null;
        const stop = () => {
          if (timer) clearInterval(timer);
          if (liveBanner === card) liveBanner = null;
          card.remove();
        };
        const paint = () => {
          if (!card.isConnected || !host.open) return stop();
          const view = describeState(guardInfo(), false);
          if (!view) return stop();
          card.className =
            "rg-card rg-inline " + BANNER_CLASS + " " + view.type +
            (alert ? " rg-hasalert" : "");
          if (alert) {
            alert.setAttribute("alert-type", view.type);
            alert.setAttribute("title", view.title);
            alert.innerHTML = view.body;
          } else {
            head.textContent = view.title;
            body.innerHTML = view.body;
          }
        };

        host.prepend(card);
        liveBanner = card;
        paint();
        host.addEventListener("close", stop);
        // same timing rule as everything else here: ha-alert has not rendered
        // on this tick, so judge whether it landed a couple of frames later
        requestAnimationFrame(() =>
          requestAnimationFrame(() => {
            if (!card.isConnected) return;
            const rect = card.getBoundingClientRect();
            // a banner is only ever informational: if the dialog gives it no
            // room, drop it rather than floating it loose on the page
            if (rect.height < 20 || rect.width < 40) stop();
          })
        );
        timer = setInterval(paint, TICK_MS);
      } catch (err) {
        /* a banner is never worth breaking a repair over */
      }
    };
    attach();
  }

  async function noteFixFlow(path, parameters, result) {
    if (!/^repairs\/issues\/fix\/?$/.test(String(path || ""))) return;
    const flowId = result && result.flow_id;
    if (!flowId) return;
    if (!(await issueIsRestarty(parameters || {}))) return;
    restartFlows.add(flowId);
    if (restartFlows.size > 32) {
      restartFlows.delete(restartFlows.values().next().value);
    }
    showRepairBanner(); // say it now, not after they commit
  }

  function classifyApi(method, path) {
    if (String(method || "").toLowerCase() !== "post") return null;
    const match = /^repairs\/issues\/fix\/(.+)$/.exec(String(path || ""));
    return match && restartFlows.has(match[1]) ? ACTIONS.restart : null;
  }

  // ------------------------------------------------------------ confirm box
  const MODAL_STYLE_ID = "restart-guard-modal-style";

  const MODAL_CSS = `
      /*
       * Home Assistant's own dialogs are native <dialog showModal()> elements,
       * which live in the browser's top layer: no z-index can get above them,
       * and everything outside them is inert, so a plain overlay would render
       * behind an open dialog and could not even be clicked. Being a modal
       * <dialog> ourselves is the only way over the top of one - the top layer
       * stacks by the order things were opened, and we open last.
       */
      .rg-modal {
        border: none; padding: 0; background: transparent; overflow: visible;
        width: calc(100% - 32px); max-width: 520px; max-height: none;
      }
      .rg-modal::backdrop { background: rgba(0, 0, 0, 0.55); }
      /* fallback for anything without native modal dialogs */
      .rg-overlay {
        position: fixed; inset: 0; z-index: 2147483000;
        display: flex; align-items: center; justify-content: center;
        background: rgba(0, 0, 0, 0.55); padding: 16px;
      }
      .rg-card {
        width: 100%; max-width: 520px; box-sizing: border-box;
        padding: 20px 24px 12px;
        border-radius: var(--ha-card-border-radius, 12px);
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #212121);
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
        font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
      }
      .rg-card .rg-head {
        font-size: 20px; font-weight: 600; margin-bottom: 10px; line-height: 1.3;
      }
      .rg-card.error .rg-head { color: var(--error-color, #db4437); }
      .rg-card.success .rg-head { color: var(--success-color, #43a047); }
      .rg-card .rg-body {
        font-size: 14px; line-height: 1.55; max-height: 50vh; overflow: auto;
      }
      .rg-card .rg-line { display: block; }
      .rg-card .rg-hint { display: block; margin-top: 8px; opacity: 0.75; }
      ${ROW_CSS}
      /*
       * ha-alert variant: the box the restart dialog uses, so every surface
       * looks the same. It carries its own padding and colour, so the card
       * only has to stop adding its own on top.
       */
      .rg-card .rg-alert { display: block; }
      .rg-card .rg-alert .rg-line { display: block; }
      .rg-card .rg-alert .rg-hint { display: block; margin-top: 8px; opacity: 0.75; }
      /*
       * ha-alert paints a translucent tint, so floating over the page it needs
       * something opaque behind it: keep the notice's own surface and drop only
       * its padding and border. Inline, the host dialog is already that surface.
       */
      .rg-notice.rg-bare {
        padding: 0; border: none; overflow: hidden;
      }
      .rg-notice.rg-bare.rg-inline {
        background: none; box-shadow: none;
      }
      .rg-notice .rg-alert { display: block; }
      .rg-actions {
        display: flex; justify-content: flex-end; gap: 8px;
        margin: 18px -8px 0;
      }
      /*
       * !important is not decoration here: inline, these buttons live inside
       * somebody else's shadow root, whose own button rules apply to them and
       * would otherwise decide how they look - or make them unreadable.
       */
      .rg-card .rg-btn {
        appearance: none !important; cursor: pointer !important;
        border: none !important; box-shadow: none !important;
        background: none !important; border-radius: 4px !important;
        margin: 0 !important; padding: 10px 12px !important;
        min-width: 0 !important; width: auto !important; height: auto !important;
        font: inherit !important; font-size: 14px !important;
        font-weight: 500 !important; letter-spacing: 0.02em !important;
        line-height: 1.2 !important; text-transform: uppercase !important;
        color: var(--primary-color, #03a9f4) !important;
      }
      .rg-card .rg-btn:hover {
        background: rgba(127, 127, 127, 0.14) !important;
      }
      .rg-card .rg-btn.danger { color: var(--error-color, #db4437) !important; }

      /*
       * Inline variant: the same card, dropped into a dialog Home Assistant
       * already has open, so there is one dialog to read instead of two.
       */
      .rg-card.rg-inline {
        width: auto; max-width: none; box-shadow: none;
        margin: 0 0 4px; padding: 12px 16px;
        border-radius: 8px; border: 1px solid transparent;
        /*
         * A full-screen dialog - which is what Home Assistant uses on a phone -
         * is a flex column whose children are sized to fill the height, and a
         * prepended child with nothing pinning it is simply shrunk away to
         * nothing. That is exactly how this went missing on mobile while
         * working on a desktop, where the dialog is only as tall as it needs.
         */
        flex: 0 0 auto !important;
        align-self: stretch !important;
        order: -1 !important;
        box-sizing: border-box !important;
        position: relative !important;
        z-index: 1 !important;
        max-height: 60vh; overflow: auto;
      }
      /*
       * The tinted card is the *fallback* look, for when ha-alert is missing.
       * With ha-alert the alert already is the coloured box, and tinting the
       * card as well stacks two of them - the buttons end up sitting on the
       * tint, and inline it reads as a separate panel bolted above the dialog
       * instead of a banner inside it.
       */
      .rg-card.rg-inline.error:not(.rg-hasalert) {
        background: rgba(219, 68, 55, 0.12); border-color: rgba(219, 68, 55, 0.55);
      }
      .rg-card.rg-inline.success:not(.rg-hasalert) {
        background: rgba(67, 160, 71, 0.12); border-color: rgba(67, 160, 71, 0.45);
      }
      /*
       * Inline with an alert: no chrome at all, so it looks exactly like the
       * banner in the 3-dot restart dialog - one box, on the host's surface.
       *
       * The gutter matters. With no padding the alert runs to the dialog's own
       * edges, which looks like a torn-off strip rather than part of the
       * dialog, and the negative margin on the button row then pushes the
       * content wider than the box and raises a horizontal scrollbar. 16px
       * matches the inset the banner uses in the 3-dot dialog.
       */
      .rg-card.rg-inline.rg-hasalert {
        background: none !important;
        border: none !important;
        box-shadow: none !important;
        padding: 16px 16px 8px !important;
        margin: 0 !important;
        overflow-x: hidden !important;
      }
      /*
       * Standing alone, the card keeps its opaque surface: ha-alert paints a
       * translucent tint, and over our own backdrop that would go muddy.
       */
      .rg-card.rg-hasalert:not(.rg-inline) { padding: 12px 12px 4px; }
      .rg-card.rg-inline .rg-head { font-size: 16px; margin-bottom: 6px; }
      .rg-card.rg-inline .rg-body { font-size: 13px; max-height: 40vh; }
      .rg-card.rg-inline .rg-actions { margin-top: 10px; }

      /* the "checked it, going ahead" note */
      .rg-notice {
        position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
        z-index: 2147483000; max-width: 90vw; box-sizing: border-box;
        padding: 10px 16px; border-radius: 8px; font-size: 13px; line-height: 1.4;
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #212121);
        border: 1px solid rgba(67, 160, 71, 0.5);
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);
        font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
      }
      /* inside an open dialog, same trick as the card: flow, do not float */
      .rg-notice.rg-inline {
        position: static; transform: none; max-width: none; margin: 0 0 4px;
        box-shadow: none; background: rgba(67, 160, 71, 0.12);
        flex: 0 0 auto !important; align-self: stretch !important;
        order: -1 !important;
      }
    `;

  function modalStyle() {
    if (document.getElementById(MODAL_STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = MODAL_STYLE_ID;
    style.textContent = MODAL_CSS;
    document.head.appendChild(style);
  }

  /*
   * The topmost dialog Home Assistant currently has open, searching through
   * shadow roots. Native modal dialogs only: those are the ones that would
   * otherwise cover us, and they are the ones worth living inside.
   */
  function openModalHost() {
    const found = [];
    const walk = (node) => {
      let dialogs;
      try {
        dialogs = node.querySelectorAll("dialog");
      } catch (err) {
        return;
      }
      for (const dialog of dialogs) {
        // never treat one of our own dialogs as a host: a prompt would nest
        // inside the previous prompt, and a banner would land in one too
        if (dialog.classList && dialog.classList.contains("rg-modal")) continue;
        const modal = dialog.matches ? dialog.matches(":modal") : dialog.open;
        if (dialog.open && modal) found.push(dialog);
      }
      for (const el of node.querySelectorAll("*")) {
        if (el.shadowRoot) walk(el.shadowRoot);
      }
    };
    try {
      walk(document);
    } catch (err) {
      return null;
    }
    return found.length ? found[found.length - 1] : null;
  }

  /*
   * The host dialog's own close control - the X in its header.
   *
   * Cancelling has to dismiss the whole dialog, not just our card. The call we
   * refuse is the one that dialog was waiting on, so leaving it open reports an
   * error for a request the user deliberately cancelled. Clicking the X lets
   * Home Assistant tear the flow down the way it expects; calling close() on
   * the native <dialog> skips that and can leave the flow dangling.
   *
   * The X is not a descendant of the native <dialog> - it is slotted light DOM
   * belonging to an ancestor component - so this climbs out through the shadow
   * roots and checks each level.
   */
  function hostCloseControl(node) {
    const seen = new Set();
    let current = node;
    for (let hop = 0; hop < 8 && current; hop++) {
      const rootNode = current.getRootNode && current.getRootNode();
      const host = rootNode && rootNode.host;
      if (!host || seen.has(host)) break;
      seen.add(host);
      // do not climb out of the dialog and start clicking the app's own chrome
      if (host.tagName && host.tagName.toLowerCase() === "home-assistant") break;
      const scope = host.shadowRoot;
      if (scope) {
        const candidates = scope.querySelectorAll(
          '[dialogaction="close"], ha-icon-button, mwc-icon-button, button'
        );
        for (const el of candidates) {
          const box = el.getBoundingClientRect();
          // ha-dialog carries a zero-sized icon button that is not the X
          if (box.width < 8 || box.height < 8) continue;
          if (el.getAttribute("dialogaction") === "close") return el;
          const own = (
            el.getAttribute("aria-label") || el.getAttribute("label") || ""
          ).toLowerCase();
          if (own.includes("close")) return el;
          const inner =
            el.shadowRoot && el.shadowRoot.querySelector("[aria-label]");
          const label = inner
            ? (inner.getAttribute("aria-label") || "").toLowerCase()
            : "";
          if (label.includes("close")) return el;
        }
      }
      current = host;
    }
    return null;
  }

  /** Close the dialog we are living inside. True if its own X was used. */
  function dismissHost(nativeDialog) {
    try {
      const control = hostCloseControl(nativeDialog);
      if (control) {
        control.click();
        return true;
      }
    } catch (err) {
      /* fall through to the blunt instrument */
    }
    try {
      if (nativeDialog && nativeDialog.open) nativeDialog.close();
    } catch (err) {
      /* nothing else to try */
    }
    return false;
  }

  /** Resolves true to let the action through, false to cancel it. */
  function showConfirm(action) {
    return new Promise((resolve) => {
      modalStyle();

      const card = document.createElement("div");
      card.className = "rg-card error";
      // Same green/red box as the restart dialog banner: ha-alert, with its
      // own colours, icon and dark-mode handling. The hand-styled head/body is
      // only the fallback for when ha-alert is not registered.
      const alert = customElements.get("ha-alert")
        ? document.createElement("ha-alert")
        : null;
      if (alert) {
        alert.className = "rg-alert";
        // marks the card as "the alert is the box", so the stylesheet knows not
        // to paint a second one behind it
        card.classList.add("rg-hasalert");
      }
      const head = document.createElement("div");
      head.className = "rg-head";
      const body = document.createElement("div");
      body.className = "rg-body";
      const actions = document.createElement("div");
      actions.className = "rg-actions";
      const cancel = document.createElement("button");
      // type matters: a button defaults to submit, and inline these live inside
      // Home Assistant's own dialog - often inside its <form> - where a submit
      // would post that form or close the dialog out from under us
      cancel.type = "button";
      cancel.className = "rg-btn";
      cancel.textContent = "Cancel";
      const go = document.createElement("button");
      go.type = "button";
      go.className = "rg-btn danger";
      go.textContent = action.confirm;
      actions.append(cancel, go);
      card.append(...(alert ? [alert] : [head, body]), actions);

      // a dialog Home Assistant already has open usually lives in a shadow
      // root, where a stylesheet in document.head cannot reach: carry a copy in
      const scoped = document.createElement("style");
      scoped.textContent = MODAL_CSS;
      card.prepend(scoped);

      let host = openModalHost(); // null once we are standing on our own
      // standAlone() clears `host`; this keeps the dialog we were placed in, so
      // cancel can still close it
      const hostDialog = host;
      let overlay = null;
      let native = false;
      let timer = null;
      let done = false;

      const close = (allow) => {
        if (done) return;
        done = true;
        // an approval given here is what the grace period carries downstream:
        // set it nowhere else, or an unguarded call would open a blind window
        if (allow) allowOnce(action);
        if (timer) clearInterval(timer);
        document.removeEventListener("keydown", onKey, true);
        if (overlay) {
          if (native && overlay.open) overlay.close();
          overlay.remove();
        } else {
          card.remove();
          // Cancelling inside somebody else's dialog: shut that dialog too.
          // The call it was waiting on is the one we just refused, so leaving
          // it open would surface an error for a deliberate cancellation.
          if (!allow && hostDialog) dismissHost(hostDialog);
        }
        resolve(allow);
      };

      const onKey = (event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          close(false);
        }
      };

      const paint = () => {
        // the host dialog closed under us (X, Escape): treat that as a cancel,
        // or the call that is waiting on this promise would hang forever
        if (host && (!card.isConnected || !host.open)) {
          close(false);
          return;
        }
        const view = describeState(guardInfo(), false);
        if (!view) {
          // sensor vanished mid-prompt: nothing left to warn about
          close(true);
          return;
        }
        card.className =
          "rg-card " + view.type + (host ? " rg-inline" : "") +
          (alert ? " rg-hasalert" : "");
        const html =
          view.body +
          (action.note ? `<span class="rg-hint">${esc(action.note)}</span>` : "");
        if (alert) {
          alert.setAttribute("alert-type", view.type);
          alert.setAttribute("title", view.title);
          alert.innerHTML = html;
        } else {
          head.textContent = view.title;
          body.innerHTML = html;
        }
        // once it is clear, this stops being an override and becomes a go-ahead
        go.textContent = view.type === "error" ? action.confirm : action.proceed;
        go.classList.toggle("danger", view.type === "error");
      };

      /** Put the card in a modal dialog of our own, above everything. */
      const standAlone = () => {
        host = null; // stops the host-closed check, and the inline styling
        native =
          typeof HTMLDialogElement !== "undefined" &&
          typeof HTMLDialogElement.prototype.showModal === "function";
        overlay = document.createElement(native ? "dialog" : "div");
        overlay.className = native ? "rg-modal" : "rg-overlay";
        overlay.appendChild(card);
        overlay.addEventListener("click", (event) => {
          if (event.target === overlay) close(false); // the backdrop
        });
        if (native) {
          // Escape on a native dialog: keep the cancel, drop the default close
          overlay.addEventListener("cancel", (event) => {
            event.preventDefault();
            close(false);
          });
        } else {
          document.addEventListener("keydown", onKey, true);
        }
        document.body.appendChild(overlay);
        if (native) overlay.showModal();
        paint();
      };

      /*
       * Whether the card we just placed actually came out on screen. A host
       * dialog decides its own layout, and some give a prepended child no room
       * at all - a full-screen mobile dialog is a flex column whose children
       * fill the height, and ours gets squeezed to nothing. A card nobody can
       * see is worse than a second dialog, because the restart just hangs.
       */
      const MIN_USABLE_HEIGHT = 60; // a real card is 100-200px; a squeezed one ~26
      const landed = () => {
        const rect = card.getBoundingClientRect();
        return (
          rect.height > MIN_USABLE_HEIGHT &&
          rect.width > 40 &&
          rect.bottom > 0 &&
          rect.top < (window.innerHeight || document.documentElement.clientHeight)
        );
      };

      cancel.addEventListener("click", () => close(false));
      go.addEventListener("click", () => close(true));

      if (host) {
        host.addEventListener("close", () => {
          if (host) close(false); // ignored once we have moved out on our own
        });
        // place first, then paint: paint() reads whether the card is still in
        // the document, and an unplaced card looks exactly like a closed host
        // the banner has said its piece; the prompt takes over from here
        const banner = host.querySelector("." + BANNER_CLASS);
        if (banner) banner.remove();
        host.prepend(card);
        paint();
        // Same timing trap as the note: when ha-alert carries the content, all
        // that measures on this tick is the button row - under the threshold -
        // so an immediate check would throw the card out of a dialog that was
        // hosting it perfectly well. Let it render, then judge.
        const settle = () => {
          if (done || !card.isConnected || overlay) return;
          if (!landed()) {
            card.remove();
            standAlone();
          }
        };
        requestAnimationFrame(() => requestAnimationFrame(settle));
      } else {
        standAlone();
      }

      if (!done) {
        cancel.focus();
        card.scrollIntoView({ block: "nearest" });
        timer = setInterval(paint, TICK_MS);
      }
    });
  }

  /**
   * Say, briefly, that the guard looked and decided not to stand in the way.
   * Goes inside an open dialog when there is one, for the same top-layer
   * reason the confirm box does.
   */
  let lastNotice = { text: "", at: 0 };

  function notice(text) {
    if (!SHOW_ALLOWED_NOTICE) return;
    // a banner is already showing this verdict, in the dialog being looked at:
    // a second green box underneath it says nothing new
    if (liveBanner && liveBanner.isConnected) return;
    // belt and braces: whatever else goes wrong, never say the same thing twice
    const at = Date.now();
    if (text === lastNotice.text && at - lastNotice.at < 3000) return;
    lastNotice = { text: text, at: at };
    try {
      modalStyle();
      const host = openModalHost();
      const el = document.createElement("div");
      el.className = "rg-notice" + (host ? " rg-inline" : "");
      if (host) {
        const scoped = document.createElement("style");
        scoped.textContent = MODAL_CSS;
        el.appendChild(scoped);
      }
      // the same green box the dialog shows when it is safe to restart
      if (customElements.get("ha-alert")) {
        const alert = document.createElement("ha-alert");
        alert.className = "rg-alert";
        alert.setAttribute("alert-type", "success");
        alert.textContent = text;
        el.classList.add("rg-bare"); // ha-alert brings its own box
        el.appendChild(alert);
      } else {
        const label = document.createElement("span");
        label.textContent = text;
        el.appendChild(label);
      }
      (host || document.body).prepend(el);
      // A host dialog can leave it with no room, exactly as it can the card,
      // and a note nobody can see is the same as no note at all. Measure a
      // couple of frames later, though: ha-alert has not rendered on this tick,
      // so an immediate read is zero and would evict a perfectly good note.
      if (host) {
        const settle = () => {
          if (!el.isConnected) return;
          const rect = el.getBoundingClientRect();
          if (rect.height < 8 || rect.width < 20 || rect.bottom <= 0) {
            el.classList.remove("rg-inline");
            document.body.prepend(el);
          }
        };
        requestAnimationFrame(() => requestAnimationFrame(settle));
      }
      setTimeout(() => el.remove(), 4000);
    } catch (err) {
      /* a note is never worth breaking a restart over */
    }
  }

  function sleep(ms) {
    return new Promise((done) => setTimeout(done, ms));
  }

  /** True to go ahead. Asks only when the guard actually has a warning. */
  async function shouldProceed(action) {
    let info = guardInfo();
    if (!info || info.missing) {
      notice("Restart Guard: no sensor to check — going ahead");
      allowOnce(action, HANDOFF_MS); // decided: the layer below must not re-ask
      return true; // no sensor: never in the way
    }

    // the sensor only polls every 30 s, so get a fresh answer before deciding
    try {
      const hass = root() && root().hass;
      const call = (hass && hass.__rgCallService) || (hass && hass.callService);
      if (call && info.entityId) {
        await call("homeassistant", "update_entity", { entity_id: info.entityId });
        await sleep(400); // let the new state land in hass.states
      }
    } catch (err) {
      /* stale data is still data */
    }

    info = guardInfo();
    if (!info || info.missing) {
      notice("Restart Guard: no sensor to check — going ahead");
      allowOnce(action, HANDOFF_MS);
      return true;
    }
    if (!info.warn) {
      notice(
        info.count
          ? `Restart Guard: nothing at risk — next run ${relative(info.mins)}`
          : `Restart Guard: nothing scheduled ${horizon(info.lookahead)} — going ahead`
      );
      // One action is one decision. Without this the same restart is judged
      // again on its way down to the websocket, and says so a second time.
      allowOnce(action, HANDOFF_MS);
      return true;
    }
    return showConfirm(action);
  }

  function cancelError(action) {
    const err = new Error(action.cancelled);
    err.code = "restart_guard_cancelled";
    return err;
  }

  // ------------------------------------------------------------- patching
  function patchHass() {
    const hass = root() && root().hass;
    if (!hass) return;

    // Service calls: homeassistant.restart, update.install, hassio.host_*.
    // hass is rebuilt by spreading the old object, so the wrapper survives.
    if (typeof hass.callService === "function" && !hass.__rgCallService) {
      const original = hass.callService.bind(hass);
      try {
        hass.callService = async function (domain, service, data, target, ...rest) {
          const action = classifyService(domain, service, data, target);
          if (action && !bypassed(action)) {
            if (!(await shouldProceed(action))) throw cancelError(action);
          }
          return original(domain, service, data, target, ...rest);
        };
        hass.__rgCallService = original;
      } catch (err) {
        /* hass not writable on this core version: the other hooks still apply */
      }
    }

    // Supervisor endpoints, and any service call that skips hass.callService.
    const conn = hass.connection;
    if (conn && typeof conn.sendMessagePromise === "function" && !conn.__rgPatched) {
      const original = conn.sendMessagePromise.bind(conn);
      try {
        conn.sendMessagePromise = async function (message, ...rest) {
          const action = classifyMessage(message);
          if (action && !bypassed(action)) {
            if (!(await shouldProceed(action))) throw cancelError(action);
          }
          return original(message, ...rest);
        };
        conn.__rgPatched = true;
      } catch (err) {
        /* leave the connection alone */
      }
    }

    // Repair fix flows, which restart from the backend.
    if (typeof hass.callApi === "function" && !hass.__rgCallApi) {
      const original = hass.callApi.bind(hass);
      try {
        hass.callApi = async function (method, path, parameters, ...rest) {
          const action = classifyApi(method, path);
          if (action && !bypassed(action)) {
            if (!(await shouldProceed(action))) throw cancelError(action);
          }
          const result = await original(method, path, parameters, ...rest);
          try {
            await noteFixFlow(path, parameters, result);
          } catch (err) {
            /* never let bookkeeping break a real call */
          }
          return result;
        };
        hass.__rgCallApi = original;
      } catch (err) {
        /* same as above */
      }
    }
  }

  // ------------------------------------------------------- open detection
  /** The dialog body. Only present while the dialog is actually open. */
  function contentEl(host) {
    return host.shadowRoot ? host.shadowRoot.querySelector(".content") : null;
  }

  function isOpen(host) {
    const content = contentEl(host);
    return !!content && content.getBoundingClientRect().height > 0;
  }

  // ------------------------------------------------------------ managing
  function manage(host) {
    if (host.__rgManaged) return;
    host.__rgManaged = true;

    let parts = null;
    let armed = false;
    let wasOpen = false;
    let lastSig = null;

    const build = () => {
      const shadow = host.shadowRoot;
      if (!shadow) return false;
      const content = contentEl(host);
      if (!content) return false;

      // the dialog body is destroyed on close, so our nodes may be detached
      if (parts && parts.wrap && parts.wrap.isConnected) return true;
      const existing = shadow.querySelector("." + WRAP_CLASS);
      if (existing && existing.isConnected) {
        parts = {
          wrap: existing,
          alert: existing.querySelector("ha-alert"),
          box: existing.querySelector("." + CLASS),
        };
        return true;
      }

      if (!shadow.querySelector("." + CLASS + "-style")) {
        shadow.appendChild(styleTag());
      }

      // inset so it lines up with the rows below instead of going edge to edge
      const wrap = document.createElement("div");
      wrap.className = WRAP_CLASS;
      wrap.style.margin = "0 16px 8px";
      wrap.hidden = true;

      // ha-alert gives native colours, icons and dark-mode handling
      let alert = null;
      let box = null;
      if (customElements.get("ha-alert")) {
        alert = document.createElement("ha-alert");
        wrap.appendChild(alert);
      } else {
        box = document.createElement("div");
        box.className = CLASS;
        wrap.appendChild(box);
      }
      parts = { wrap: wrap, alert: alert, box: box };
      content.prepend(wrap);

      // one listener for the lifetime of this dialog body
      content.addEventListener(
        "click",
        (event) => {
          const row = event
            .composedPath()
            .find(
              (node) =>
                node.tagName &&
                node.tagName.toLowerCase() === "ha-list-item-button"
            );
          if (!row) return;
          const headline = (
            (row.querySelector('[slot="headline"]') || {}).textContent || ""
          ).trim();
          if (/quick reload/i.test(headline)) return; // reload interrupts nothing
          const info = guardInfo();
          if (!info || info.missing || !info.warn || armed) {
            // this banner has already had its say, so don't also stop the
            // service call it is about to make - but it vouches for a restart
            // only, never for an update that happens to follow it
            allowOnce(ACTIONS.restart);
            return;
          }
          event.preventDefault();
          event.stopPropagation();
          armed = true;
          lastSig = null; // force an immediate repaint
          tick();
          if (parts && parts.wrap) parts.wrap.scrollIntoView({ block: "nearest" });
        },
        true // capture: runs before the dialog's own handler
      );
      return true;
    };

    const tick = () => {
      if (!host.isConnected) return;
      const open = isOpen(host);

      if (open && !wasOpen) {
        // freshly opened: forget any previous arming, ask for a fresh value
        armed = false;
        lastSig = null;
        const info = guardInfo();
        if (info && !info.missing) forceRefresh(info.entityId);
      }
      wasOpen = open;
      if (!open) return;
      if (!build()) return;

      const view = describeState(guardInfo(), armed);
      const sig = signature(view);
      if (sig === lastSig) return; // nothing changed, leave the DOM alone
      lastSig = sig;
      apply(parts, view);
    };

    // instant: the dialog body being rebuilt means it just opened
    if (host.shadowRoot) {
      new MutationObserver(tick).observe(host.shadowRoot, { childList: true });
    }
    // steady: keeps the countdown live while the dialog sits open
    setInterval(tick, TICK_MS);
    tick();
  }

  // -------------------------------------------------------------- observe
  function attach() {
    const el = root();
    if (!el || !el.shadowRoot) return setTimeout(attach, 250);

    // hass is replaced on every reconnect, so keep checking the hooks are on
    patchHass();
    setInterval(patchHass, 2000);

    const existing = el.shadowRoot.querySelector(DIALOG_TAG);
    if (existing) manage(existing);

    new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.tagName && node.tagName.toLowerCase() === DIALOG_TAG) {
            manage(node);
          }
        }
      }
    }).observe(el.shadowRoot, { childList: true });
  }

  attach();
  window.__restartGuardLoaded = true; // handy marker when debugging in DevTools
  window.__restartGuardBuild = "0.0.4"; // which build the browser actually has
})();
