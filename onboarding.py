"""First-run setup tour. A cover screen (big logo, the pitch, and the Dexcom
login if it isn't connected yet), then five screens that explain the lights
and build the settings one step at a time, around an animated sketch of the
button and a sample day of glucose. Shown automatically on a fresh install
and available again later from the settings page.

Each step saves its own settings (via settings_form.parse_settings_form, the
same validation as the full settings page), so leaving part-way through
keeps what was chosen so far.
"""

import json

from flask import redirect, render_template_string, request, url_for

from config import DAY_NAMES, ON_CALL_DURATION_CHOICES_MINUTES, RAMP_SENSITIVITY_MG_DL
from settings_form import parse_settings_form

# Step 1 is the cover; the progress bar covers steps 2-6.
PROGRESS_LABELS = ["How it works", "Active window", "High levels", "Ramp detection", "On-demand"]
LAST_STEP = 1 + len(PROGRESS_LABELS)
STEP_GROUPS = {1: (), 2: (), 3: ("window",), 4: ("thresholds",), 5: ("ramp",), 6: ("on_call",)}
SCENES = {2: "lights", 3: "window", 4: "levels", 5: "ramp", 6: "ondemand"}
RAMP_BLURBS = {
    "high": "Earliest warning, more false alarms",
    "medium": "A good balance for most people",
    "low": "Only clear, fast climbs",
    "off": "No white light; the button waits for yellow",
}
# Where the on-demand illustration's double press happens.
ON_DEMAND_EXAMPLE_START = "21:30"

SETUP_BODY = """
<style>
  .setup-steps { list-style: none; display: flex; gap: 6px; padding: 0; margin: 0 0 22px; }
  .setup-steps li {
    flex: 1; font-size: 0.72rem; color: var(--muted); padding-top: 8px;
    border-top: 3px solid var(--border); line-height: 1.25;
  }
  .setup-steps li.done { border-color: #9DB8E8; }
  .setup-steps li.current { border-color: var(--blue); color: var(--ink); font-weight: 600; }
  .setup-title { font-size: 1.5rem; line-height: 1.22; margin: 0 0 8px; color: var(--ink); }
  .setup-lede { margin: 0 0 18px; color: #4A5560; line-height: 1.5; font-size: 0.98rem; }
  .scene { margin: 0 -8px 6px; }
  #scene { display: block; width: 100%; height: auto; overflow: visible; }
  .scene-caption {
    display: flex; align-items: center; justify-content: center; gap: 8px; min-height: 1.4em;
    margin: 0 0 18px; font-size: 0.85rem; color: #4A5560; font-variant-numeric: tabular-nums;
  }
  .cap-dot { width: 11px; height: 11px; border-radius: 50%; border: 1.5px solid var(--ink); background: #fff; flex: none;
             transition: box-shadow 0.2s; }
  .setup-form fieldset { margin-bottom: 14px; }
  .legend { list-style: none; padding: 0; margin: 0 0 6px; display: grid; gap: 6px;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); }
  .legend li { display: flex; align-items: flex-start; gap: 11px; font-size: 0.9rem; line-height: 1.35;
               padding: 9px 12px; border: 1.5px solid transparent; border-radius: 12px;
               transition: background-color 0.2s, border-color 0.2s; }
  .legend li.on { background: #fff; border-color: var(--border); }
  .legend li b { display: block; font-size: 0.95rem; }
  .legend li small { display: block; color: var(--muted); font-size: 0.82rem; }
  .legend .dot { width: 16px; height: 16px; border-radius: 50%; border: 1.5px solid var(--ink); flex: none; margin-top: 2px; }
  .dot.white { background: #fff; box-shadow: 0 0 0 3px #fff, 0 0 7px 3px rgba(45, 35, 32, 0.28); }
  .dot.yellow { background: #F5C518; } .dot.red { background: #EB4438; }
  .dot.purple { background: #B44BE0; } .dot.blue { background: #3B82F6; }
  .choices { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); margin-top: 6px; }
  .choices.wide { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  label.choice { position: relative; margin: 0; display: block; }
  .choice input { position: absolute; inset: 0; opacity: 0; margin: 0; cursor: pointer; }
  .choice span {
    display: block; height: 100%; border: 1.5px solid var(--border); border-radius: 12px;
    padding: 10px 12px; background: #fff; transition: border-color 0.15s, box-shadow 0.15s;
  }
  .choice b { display: block; font-size: 0.95rem; }
  .choice small { display: block; color: var(--muted); font-size: 0.8rem; margin-top: 2px; line-height: 1.35; }
  .choice input:checked + span { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(45, 108, 223, 0.16); }
  .choice input:focus-visible + span { outline: 2px solid var(--blue); outline-offset: 2px; }
  .setup-nav { display: flex; justify-content: space-between; align-items: center; margin-top: 18px; gap: 12px; }
  .setup-nav button { margin-top: 0; }
  .setup-nav a { color: var(--ink); font-weight: 600; text-decoration: none; padding: 10px 4px; }
  .setup-nav a:hover { text-decoration: underline; }
  .setup-after { margin: 10px 0 0; font-size: 0.85rem; color: var(--muted); text-align: right; }
  .linklike { background: none; color: var(--muted); padding: 0; margin: 22px 0 0; font-weight: 500;
              font-size: 0.85rem; text-decoration: underline; }
  .linklike:hover { background: none; color: var(--ink); }
  .warn { color: var(--red-dark); font-size: 0.85rem; margin-top: 8px; }

  .s-screen { fill: #fff; stroke: var(--border); stroke-width: 1.5; }
  .scene-ramp .s-screen { fill: #F1F4F8; }
  .s-gridline { stroke: #E8EEF4; stroke-width: 1; }
  .scene-ramp .s-gridline { stroke: #E2E8EF; }
  .s-ylab, .s-xlab { font-size: 13px; fill: #8A97A4; }
  .s-band rect { fill: rgba(45, 108, 223, 0.09); }
  .s-band.strong rect { fill: rgba(45, 108, 223, 0.15); stroke: rgba(45, 108, 223, 0.5); stroke-width: 1.5; }
  .s-band.oncall rect { fill: rgba(45, 108, 223, 0.17); stroke: #2D6CDF; stroke-width: 1.6; stroke-dasharray: 6 4; }
  .s-bandlab { font-size: 13.5px; font-weight: 700; fill: #2D6CDF; }
  .s-wb { fill: #fff; stroke: var(--ink); stroke-opacity: 0.6; stroke-width: 1.5; stroke-dasharray: 6 4; }
  .s-wb-glow { fill: #9FB2D3; opacity: 0.6; }
  .s-bandlab.white { fill: var(--ink); }
  .s-th { fill: none; stroke-width: 1.6; stroke-dasharray: 7 5; }
  .s-th.strong { stroke-width: 2.6; }
  .s-th.yellow { stroke: #E0A800; } .s-th.red { stroke: #EB4438; } .s-th.low { stroke: #B44BE0; opacity: 0.65; }
  .s-thlab { font-size: 13.5px; font-weight: 700; }
  .s-thlab.yellow { fill: #A87D00; } .s-thlab.red { fill: #C22F24; } .s-thlab.low { fill: #8E33B8; }
  .s-curve-full { fill: none; stroke: #C2CDD9; stroke-width: 2; stroke-dasharray: 2 5; stroke-linecap: round; }
  .s-curve-trail { fill: none; stroke: var(--ink); stroke-width: 2.8; stroke-linecap: round; stroke-linejoin: round; }
  /* the line in the color the button shows at that moment */
  .s-seg { fill: none; stroke-width: 3.4; stroke-linecap: round; stroke-linejoin: round; }
  .s-seg.off { stroke: var(--ink); stroke-width: 2.8; }
  .s-seg.yellow { stroke: #E0A800; } .s-seg.red { stroke: #EB4438; } .s-seg.purple { stroke: #B44BE0; }
  .s-seg.white { stroke: #fff; stroke-width: 3.6; }
  .s-seg-case { fill: none; stroke: var(--ink); stroke-width: 7.4; stroke-linecap: round; stroke-linejoin: round; }
  .s-seg-glow { fill: none; stroke: #8FA6CC; stroke-width: 16; stroke-linecap: round; opacity: 0.55; }
  .s-headline { stroke: var(--ink); stroke-opacity: 0.16; stroke-width: 1; }
  .s-headdot { stroke: var(--ink); stroke-width: 2; fill: #fff; }
  #s-headglow { transition: opacity 0.2s; }
  .s-cable { fill: none; stroke: var(--ink); stroke-width: 2; stroke-linecap: round; }
  .s-base, .s-base-top { fill: #FAFBFC; stroke: var(--ink); stroke-width: 2; stroke-linejoin: round; }
  .s-dome { fill: url(#dome-grad); stroke: var(--ink); stroke-width: 2; }
  .s-gloss { fill: none; stroke: #fff; stroke-width: 3; stroke-linecap: round; opacity: 0.85; }
  .s-rays line { stroke-width: 2.8; stroke-linecap: round; }
  .s-rays { opacity: 0; transition: opacity 0.2s; }
  .s-rays.on { opacity: 1; animation: rays 1.4s ease-in-out infinite; }
  @keyframes rays { 50% { opacity: 0.45; } }
  @media (prefers-reduced-motion: reduce) { .s-rays.on { animation: none; } }
  #s-pool, #s-halo { transition: opacity 0.25s ease; }
  .s-mk { fill: #fff; stroke: var(--ink); stroke-width: 1.6; }
  .s-mk.sel { stroke-width: 2.6; }
  .s-mklab { font-size: 13.5px; font-weight: 700; fill: var(--ink); }
  .s-leader { stroke: var(--ink); stroke-width: 1.3; stroke-opacity: 0.6; }
  .s-note { font-size: 13.5px; font-weight: 600; fill: #6B7785; }
  /* a thin outline keeps chart labels readable where they cross lines */
  .s-thlab, .s-mklab, .s-note, .s-bandlab { paint-order: stroke; stroke: #fff; stroke-width: 4px; stroke-linejoin: round; }
  .scene-ramp .s-note { stroke: #F1F4F8; }

  /* the cover */
  .cover-page .brand, .cover-page #button-status { display: none; }
  .cover { text-align: center; padding-top: 4px; }
  .cover-logo { display: block; width: min(380px, 84%); height: auto; margin: 0 auto 30px; }
  .cover-title { font-size: 1.9rem; line-height: 1.18; margin: 0 0 12px; color: var(--ink); letter-spacing: -0.01em;
                 text-wrap: balance; }
  .cover-lede { margin: 0 auto 28px; max-width: 520px; color: #4A5560; line-height: 1.55; font-size: 1.02rem; }
  .cover form, .cover .error { text-align: left; }
  .btn-wide { width: 100%; margin-top: 0; padding: 13px 20px; font-size: 1rem; }
  .cover-later { margin: 14px 0 0; font-size: 0.88rem; color: var(--muted); }
  .cover-later a { color: var(--ink); font-weight: 600; }
  .connected-card {
    display: flex; align-items: center; gap: 10px; text-align: left; background: var(--card);
    border: 1px solid var(--border); border-radius: 14px; padding: 14px 18px; margin-bottom: 16px;
  }
  .connected-card .status-dot { background: #2FAE66; box-shadow: 0 0 6px #2FAE66; }
</style>

{% if step == 1 %}
<script>document.body.classList.add("cover-page");</script>
<div class="cover">
  <img class="cover-logo" src="data:image/png;base64,{{ cover_logo }}" alt="Danko Panko's HyLight">
  <h1 class="cover-title">{{ title }}</h1>
  <p class="cover-lede">{{ lede }}</p>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}

  {% if connected %}
  <div class="connected-card"><span class="status-dot"></span>
    <span>Connected to Dexcom as <b>{{ username }}</b></span></div>
  <form method="post" action="{{ url_for('setup', step=1) }}">
    <button type="submit" class="btn-primary btn-wide">{{ "Start the tour" if tour else "Get started" }}</button>
  </form>
  <form method="post" action="{{ url_for('setup_exit') }}">
    <button type="submit" class="linklike">{{ "Exit the tour" if tour else "Skip setup" }}</button>
  </form>
  {% else %}
  <form method="post" action="{{ url_for('setup', step=1) }}" id="connect-form">
    <fieldset>
      <legend>Connect your Dexcom account</legend>
      <label for="username">Username</label>
      <input type="text" id="username" name="username" value="{{ username or '' }}" required>

      <label for="password">Password</label>
      <input type="password" id="password" name="password" required>

      <label for="region">Region</label>
      <select id="region" name="region">
        {% for value, name in regions %}
        <option value="{{ value }}" {% if value == region %}selected{% endif %}>{{ name }}</option>
        {% endfor %}
      </select>
      <div class="hint">
        Sign in with the Dexcom account of the person wearing the sensor
        &mdash; not a follower's account. In the Dexcom app: Settings &rarr;
        Share &rarr; turn Share on and add a follower.
      </div>
    </fieldset>
    <button type="submit" class="btn-primary btn-wide">Connect and continue</button>
  </form>
  <p class="cover-later"><a href="{{ url_for('setup', step=2) }}">Connect later</a> and look around first</p>
  <script>
  (function () {
    var form = document.getElementById("connect-form"), btn = form.querySelector("button");
    form.addEventListener("submit", function () { btn.disabled = true; btn.textContent = "Connecting\\u2026"; });
    window.addEventListener("pageshow", function () { btn.disabled = false; btn.textContent = "Connect and continue"; });
  })();
  </script>
  {% endif %}
</div>

{% else %}
<div class="setup">
  <ol class="setup-steps" aria-label="Setup progress">
    {% for label in progress_labels %}{% set n = loop.index + 1 %}
    <li class="{% if n == step %}current{% elif n < step %}done{% endif %}"
        {% if n == step %}aria-current="step"{% endif %}>{{ label }}</li>
    {% endfor %}
  </ol>

  <h1 class="setup-title">{{ title }}</h1>
  <p class="setup-lede">{{ lede }}</p>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}

  <div class="scene">
    <svg id="scene" class="scene-{{ scene }}" viewBox="0 0 600 330" role="img" aria-label="{{ scene_label }}">
      <defs>
        <filter id="sketchy" x="-10%" y="-10%" width="120%" height="120%">
          <feTurbulence type="fractalNoise" baseFrequency="0.04" numOctaves="2" seed="4" result="noise"/>
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="2.4" xChannelSelector="R" yChannelSelector="G"/>
        </filter>
        <filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="9"/></filter>
        <filter id="soft-sm" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="5"/></filter>
        <radialGradient id="pool-grad">
          <stop class="pool-stop" offset="0" stop-color="#fff" stop-opacity="0.75"/>
          <stop class="pool-stop" offset="1" stop-color="#fff" stop-opacity="0"/>
        </radialGradient>
        <radialGradient id="dome-grad" cx="0.45" cy="0.35" r="0.8">
          <stop id="dome-a" offset="0" stop-color="#FFFFFF"/>
          <stop id="dome-b" offset="1" stop-color="#E9EDF2"/>
        </radialGradient>
        <clipPath id="plot-clip"><rect x="62" y="14" width="504" height="176"/></clipPath>
        <clipPath id="clip-trail"><rect id="clip-trail-r" x="0" y="0" width="0" height="330"/></clipPath>
      </defs>

      <ellipse id="s-pool" cx="300" cy="304" rx="260" ry="44" fill="url(#pool-grad)" opacity="0"/>
      <rect class="s-screen" x="12" y="4" width="576" height="222" rx="16"/>
      <g id="s-grid"></g>
      <g clip-path="url(#plot-clip)"><g id="s-bands"></g><g id="s-whitebox"></g><g id="s-oncall"></g></g>
      <g id="s-thresholds"></g>
      <g clip-path="url(#plot-clip)">
        <path id="s-curve-full" class="s-curve-full"/>
        <path id="s-curve-trail" class="s-curve-trail" filter="url(#sketchy)"/>
        <g clip-path="url(#clip-trail)"><g id="s-trail-colored" filter="url(#sketchy)"></g></g>
      </g>
      <g id="s-markers"></g>
      <line id="s-headline" class="s-headline" y1="14" y2="190"/>
      <circle id="s-headglow" r="12" cx="-20" cy="-20" filter="url(#soft-sm)" opacity="0"/>
      <circle id="s-headdot" class="s-headdot" r="5.5" cx="-20" cy="-20"/>

      <path class="s-cable" d="M330,246 C342,236 360,232 372,226" filter="url(#sketchy)"/>
      <ellipse id="s-halo" cx="300" cy="262" rx="86" ry="32" filter="url(#soft)" fill="#fff" opacity="0"/>
      <g id="s-rays" class="s-rays" filter="url(#sketchy)">
        <line x1="384.8" y1="252.1" x2="398" y2="245.1"/><line x1="396" y1="268" x2="411" y2="268"/>
        <line x1="384.8" y1="283.9" x2="398" y2="290.9"/><line x1="215.2" y1="252.1" x2="202" y2="245.1"/>
        <line x1="204" y1="268" x2="189" y2="268"/><line x1="215.2" y1="283.9" x2="202" y2="290.9"/>
      </g>
      <g filter="url(#sketchy)">
        <path class="s-base" d="M226,280 v16 a74,22 0 0 0 148,0 v-16"/>
        <ellipse class="s-base-top" cx="300" cy="280" rx="74" ry="22"/>
        <path class="s-dome" d="M242,274 v-12 a58,19 0 0 1 116,0 v12 a58,19 0 0 1 -116,0z"/>
        <ellipse class="s-dome" cx="300" cy="262" rx="58" ry="19"/>
        <path class="s-gloss" d="M262,256 q20,-9 46,-8"/>
      </g>
    </svg>
  </div>
  <p class="scene-caption" aria-hidden="true"><span class="cap-dot" id="cap-dot"></span><span id="cap-text"></span></p>

  <form method="post" class="setup-form" action="{{ url_for('setup', step=step) }}">
    {% if scene == "lights" %}
    <ul class="legend">
      <li data-light="white"><span class="dot white"></span><span><b>White: rising</b>
        <small>Glucose has started to climb, often after a meal</small></span></li>
      <li data-light="yellow"><span class="dot yellow"></span><span><b>Yellow: high</b>
        <small>Above your first high level</small></span></li>
      <li data-light="red"><span class="dot red"></span><span><b>Red: very high</b>
        <small>Above your second high level</small></span></li>
      <li data-light="purple"><span class="dot purple"></span><span><b>Purple: low</b>
        <small>Below 70</small></span></li>
    </ul>
    <p class="hint">When you're in range, or outside the hours you choose, the button stays dark.</p>

    {% elif scene == "window" %}
    <fieldset>
      <legend>Active Window</legend>
      <div class="row">
        <div>
          <label for="window_start">Start time</label>
          <input type="time" id="window_start" name="window_start" value="{{ cfg.window_start }}" required>
        </div>
        <div>
          <label for="window_end">End time</label>
          <input type="time" id="window_end" name="window_end" value="{{ cfg.window_end }}" required>
        </div>
      </div>
      <label>Active days</label>
      <div class="days">
        {% for i, name in days %}
        <label><input type="checkbox" name="active_days" value="{{ i }}" {% if i in cfg.active_days %}checked{% endif %}> {{ name }}</label>
        {% endfor %}
      </div>
      <div class="hint">Many people pick the few hours after lunch, when spikes are most common.</div>
    </fieldset>

    {% elif scene == "levels" %}
    <fieldset>
      <legend>Warning Thresholds (mg/dL)</legend>
      <div class="row">
        <div>
          <label for="yellow_threshold">Yellow above</label>
          <input type="number" id="yellow_threshold" name="yellow_threshold" value="{{ cfg.yellow_threshold }}" min="40" max="400" required>
        </div>
        <div>
          <label for="red_threshold">Red above</label>
          <input type="number" id="red_threshold" name="red_threshold" value="{{ cfg.red_threshold }}" min="40" max="400" required>
        </div>
      </div>
      <div class="warn" id="th-warn" hidden>Red needs to be higher than yellow.</div>
      <div class="hint">Below 70 always shows purple &mdash; that low alert is fixed.</div>
    </fieldset>

    {% elif scene == "ramp" %}
    <fieldset>
      <legend>Ramp Detection Sensitivity</legend>
      <div class="choices wide">
        {% for level, mg in ramp_choices.items() %}
        <label class="choice">
          <input type="radio" name="ramp_sensitivity" value="{{ level }}" {% if level == cfg.ramp_sensitivity %}checked{% endif %}>
          <span><b>{{ level | capitalize }}{% if mg %} &middot; {{ mg }} mg/dL in 15 min{% endif %}</b><small>{{ ramp_blurbs[level] }}</small></span>
        </label>
        {% endfor %}
      </div>
      <div class="hint">Dexcom's own rising trend arrow also counts, unless this is Off.</div>
    </fieldset>

    {% elif scene == "ondemand" %}
    <fieldset>
      <legend>On-Demand Window</legend>
      <label style="margin-top:0">A double press turns the button on for</label>
      <div class="choices">
        {% for minutes in on_call_choices %}
        <label class="choice">
          <input type="radio" name="on_call_minutes" value="{{ minutes }}" {% if minutes == cfg.on_call_minutes %}checked{% endif %}>
          <span><b>{{ minutes | duration }}</b></span>
        </label>
        {% endfor %}
      </div>
    </fieldset>
    {% endif %}

    <div class="setup-nav">
      <a href="{{ url_for('setup', step=step - 1) }}">&larr; Back</a>
      <button type="submit" class="btn-primary">{{ next_label }}</button>
    </div>
    {% if step == last_step and not connected %}
    <p class="setup-after">Next, you'll connect your Dexcom account.</p>
    {% endif %}
  </form>

  <form method="post" action="{{ url_for('setup_exit') }}">
    <button type="submit" class="linklike">{{ "Exit the tour" if tour else "Skip setup" }}</button>
  </form>
</div>

<script type="application/json" id="scene-data">{{ scene_json | safe }}</script>
<script>
(function () {
  var S = JSON.parse(document.getElementById("scene-data").textContent);
  var SC = S.scene;  // "lights" | "window" | "levels" | "ramp" | "ondemand"
  var NS = "http://www.w3.org/2000/svg";
  var PX0 = 62, PX1 = 566, PY0 = 22, PY1 = 190, V0 = 40, V1 = 262;
  // The ramp screen draws its own unlabeled yellow line, high enough to leave
  // room to see the white stretch before it.
  var RAMP_YELLOW = 160;
  // Screens where the line takes the color the button shows at that moment.
  var COLORED = SC === "lights" || SC === "levels" || SC === "ramp";
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function $(id) { return document.getElementById(id); }

  // ---- a sample day: minutes since midnight -> mg/dL (monotone cubic) ----
  // Tuned so each ramp sensitivity turns the light white at a different
  // moment, all before the climb reaches the yellow line.
  var KP = [[480,112],[520,118],[550,146],[575,164],[605,150],[645,126],[690,104],[720,96],[740,94],
            [750,96.5],[760,104],[770,116.5],[780,134],[790,156],[800,179],[815,205],[835,226],[860,233],
            [890,222],[925,192],[965,160],[1010,136],[1060,123],[1120,118],[1150,124],[1180,148],[1210,166],
            [1240,158],[1270,138],[1300,126],[1320,134],[1345,156],[1370,160],[1395,146],[1425,128],[1440,122]];
  var glucose = (function (pts) {
    var n = pts.length, xs = [], ys = [], d = [], m = [], i;
    for (i = 0; i < n; i++) { xs.push(pts[i][0]); ys.push(pts[i][1]); }
    for (i = 0; i < n - 1; i++) d.push((ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]));
    m.push(d[0]);
    for (i = 1; i < n - 1; i++) m.push(d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2);
    m.push(d[n - 2]);
    for (i = 0; i < n - 1; i++) {
      if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
      var a = m[i] / d[i], b = m[i + 1] / d[i], s = a * a + b * b;
      if (s > 9) { var t = 3 / Math.sqrt(s); m[i] = t * a * d[i]; m[i + 1] = t * b * d[i]; }
    }
    return function (x) {
      if (x <= xs[0]) return ys[0];
      if (x >= xs[n - 1]) return ys[n - 1];
      var k = 0; while (xs[k + 1] < x) k++;
      var h = xs[k + 1] - xs[k], t = (x - xs[k]) / h, t2 = t * t, t3 = t2 * t;
      return (2 * t3 - 3 * t2 + 1) * ys[k] + (t3 - 2 * t2 + t) * h * m[k] +
             (-2 * t3 + 3 * t2) * ys[k + 1] + (t3 - t2) * h * m[k + 1];
    };
  })(KP);

  function toMin(s) { var p = String(s).split(":"); return (+p[0]) * 60 + (+p[1]); }
  function fmt(t) {
    t = Math.round(t);
    var h = Math.floor(t / 60) % 24, mm = t % 60, ap = h < 12 ? "AM" : "PM";
    return (h % 12 || 12) + ":" + (mm < 10 ? "0" : "") + mm + " " + ap;
  }
  function tick(t) {
    var h = Math.floor(t / 60) % 24, mm = t % 60, ap = h < 12 ? "a" : "p";
    return (h % 12 || 12) + (mm ? ":" + (mm < 10 ? "0" : "") + mm : "") + ap;
  }
  function durLabel(min) { return min < 60 ? min + " min" : min === 60 ? "1 hour" : (min / 60) + " hours"; }
  function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  var st = {
    ws: toMin(S.window_start), we: toMin(S.window_end), yellow: +S.yellow, red: +S.red,
    ramp: S.ramp, onCall: +S.on_call, ocStart: toMin(S.on_call_start)
  };

  function readInputs() {
    var e;
    if ((e = $("window_start")) && /^\\d\\d:\\d\\d$/.test(e.value)) st.ws = toMin(e.value);
    if ((e = $("window_end")) && /^\\d\\d:\\d\\d$/.test(e.value)) st.we = toMin(e.value);
    if ((e = $("yellow_threshold")) && e.value !== "" && !isNaN(+e.value)) st.yellow = +e.value;
    if ((e = $("red_threshold")) && e.value !== "" && !isNaN(+e.value)) st.red = +e.value;
    if ((e = document.querySelector('input[name="ramp_sensitivity"]:checked'))) st.ramp = e.value;
    if ((e = document.querySelector('input[name="on_call_minutes"]:checked'))) st.onCall = +e.value;
    var warn = $("th-warn");
    if (warn) warn.hidden = st.red > st.yellow;
  }

  function view() {
    if (SC === "levels") return [st.ws, Math.min(1440, Math.max(st.we, st.ws + 180))];
    if (SC === "ramp") return [735, 810];          // 12:15 - 1:30 PM, the lunch climb
    if (SC === "ondemand") return [1140, 1440];    // 7 PM - midnight
    return [480, 1440];                            // 8 AM - midnight
  }
  function X(t) { var v = view(); return PX0 + (t - v[0]) / (v[1] - v[0]) * (PX1 - PX0); }
  function Y(mg) { return PY1 - (Math.max(V0, Math.min(V1, mg)) - V0) / (V1 - V0) * (PY1 - PY0); }
  function yellowLine() { return SC === "ramp" ? RAMP_YELLOW : st.yellow; }

  // ---- the same rules the app uses ----
  function inSched(t) { return t >= st.ws && t <= st.we; }
  function inOnCall(t) { return SC === "ondemand" && t >= st.ocStart && t < st.ocStart + st.onCall; }
  // The levels and ramp screens pretend the window is always open.
  function active(t) { return SC === "levels" || SC === "ramp" || inSched(t) || inOnCall(t); }
  function rampingAt(t, mg) {
    var k = Math.floor(t / 5) * 5;  // Dexcom has a new reading every 5 minutes
    return glucose(k) - glucose(k - 15) >= mg && glucose(k - 5) - glucose(k - 20) >= mg;
  }
  function statusAt(t) {
    if (!active(t)) return "off";
    var v = glucose(t), mg = S.ramp_mg[st.ramp];
    if (v < 70) return "purple";
    if (v > st.red && SC !== "ramp") return "red";  // the ramp screen is only about white and yellow
    if (v > yellowLine()) return "yellow";
    return mg && rampingAt(t, mg) ? "white" : "off";
  }

  // ---- drawing ----
  function make(tag, attrs, parent, text) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  }
  function clear(id) { var n = $(id); while (n.firstChild) n.removeChild(n.firstChild); return n; }
  function keepInside(textEl) {
    var w = textEl.getComputedTextLength(), x = +textEl.getAttribute("x");
    if (x + w > PX1 - 4) textEl.setAttribute("x", PX1 - 4 - w);
  }
  function pathFor(a, b) {
    var d = "", stepMin = Math.max(0.5, (b - a) / 260);
    for (var t = a; t < b; t += stepMin) d += (d ? "L" : "M") + X(t).toFixed(1) + "," + Y(glucose(t)).toFixed(1);
    return d + (d ? "L" : "M") + X(b).toFixed(1) + "," + Y(glucose(b)).toFixed(1);
  }
  function band(a, b, kind, label, parent) {
    var v = view(), A = Math.max(a, v[0]), B = Math.min(b, v[1]);
    if (B <= A) return null;
    var grp = make("g", {"class": "s-band " + kind}, parent);
    make("rect", {x: X(A), y: PY0 - 10, width: X(B) - X(A), height: PY1 - PY0 + 10, rx: 7}, grp);
    if (label) make("text", {x: X(A) + 8, y: PY0 + 8, "class": "s-bandlab"}, grp, label);
    return grp;
  }
  function threshold(level, kind, strong, label, parent) {
    make("line", {x1: PX0, x2: PX1, y1: Y(level), y2: Y(level), "class": "s-th " + kind + (strong ? " strong" : "")}, parent);
    if (label) make("text", {x: PX1 - 4, y: Y(level) - 7, "text-anchor": "end", "class": "s-thlab " + kind}, parent, label);
  }

  // Splits the view into stretches where the button shows one color.
  var SEGS = [];
  function segments() {
    var v = view(), dt = (v[1] - v[0]) / 600, out = [], a = v[0], s = statusAt(v[0]);
    for (var t = v[0] + dt; t < v[1]; t += dt) {
      var n = statusAt(t);
      if (n !== s) { out.push({s: s, a: a, b: t}); a = t; s = n; }
    }
    out.push({s: s, a: a, b: v[1]});
    return out;
  }
  function drawColoredTrail(segs) {
    var g = clear("s-trail-colored");
    segs.forEach(function (sg) { if (sg.s !== "white") make("path", {d: pathFor(sg.a, sg.b), "class": "s-seg " + sg.s}, g); });
    // white goes on top, as a cased line with a soft glow so it reads on a white chart
    segs.forEach(function (sg) {
      if (sg.s !== "white") return;
      var d = pathFor(sg.a, sg.b);
      make("path", {d: d, "class": "s-seg-glow"}, g);
      make("path", {d: d, "class": "s-seg-case"}, g);
      make("path", {d: d, "class": "s-seg white"}, g);
    });
  }

  function drawOnCall(animate) {
    var grp = clear("s-oncall");
    var b = band(st.ocStart, st.ocStart + st.onCall, "oncall", "On-demand \\u00b7 " + durLabel(st.onCall), grp);
    if (!b || !animate || reduced) return;
    var start = performance.now();
    (function drop(now) {
      var p = Math.min(1, (now - start) / 520), e = 1 - Math.pow(1 - p, 3);
      b.setAttribute("transform", "translate(0," + (-70 * (1 - e)).toFixed(1) + ")");
      b.style.opacity = (0.2 + 0.8 * e).toFixed(2);
      if (p < 1) requestAnimationFrame(drop);
    })(start);
  }

  function firstRamp(mg) {
    var v = view();
    for (var k = Math.ceil(v[0] / 5) * 5; k <= v[1]; k += 5) if (rampingAt(k, mg)) return k;
    return null;
  }
  function firstAbove(level) {
    var v = view();
    for (var t = v[0]; t <= v[1]; t += 0.25) if (glucose(t) > level) return t;
    return null;
  }

  // ---- ramp screen: a white box over the stretch where the light is white ----
  function whiteSpan() {
    var mg = S.ramp_mg[st.ramp];
    if (!mg) return null;
    var tr = firstRamp(mg), ty = firstAbove(RAMP_YELLOW);
    if (tr === null || (ty !== null && tr >= ty)) return null;
    return [tr, ty === null ? view()[1] : ty];
  }
  function paintWhiteBox(span) {
    var g = clear("s-whitebox");
    if (!span) return;
    var x0 = X(span[0]), x1 = X(span[1]), y = PY0 - 10, h = PY1 - PY0 + 10;
    make("rect", {x: x0, y: y, width: x1 - x0, height: h, rx: 7, "class": "s-wb-glow", filter: "url(#soft-sm)"}, g);
    make("rect", {x: x0, y: y, width: x1 - x0, height: h, rx: 7, "class": "s-wb"}, g);
    keepInside(make("text", {x: x0 + 8, y: PY0 + 8, "class": "s-bandlab white"}, g, "Light turns white"));
  }
  var boxSpan = null, boxRun = 0;
  function drawWhiteBox(animate) {
    var from = boxSpan, to = whiteSpan(), run = ++boxRun;
    boxSpan = to;
    if (!animate || reduced || !from || !to) { paintWhiteBox(to); return; }
    var start = performance.now();
    (function slide(now) {
      if (run !== boxRun) return;
      var p = Math.min(1, (now - start) / 380), e = 1 - Math.pow(1 - p, 3);
      paintWhiteBox([from[0] + (to[0] - from[0]) * e, from[1] + (to[1] - from[1]) * e]);
      if (p < 1) requestAnimationFrame(slide);
    })(start);
  }

  function render() {
    var v = view(), grid = clear("s-grid");
    [100, 150, 200, 250].forEach(function (mg) {
      make("line", {x1: PX0, x2: PX1, y1: Y(mg), y2: Y(mg), "class": "s-gridline"}, grid);
      make("text", {x: PX0 - 8, y: Y(mg) + 4, "text-anchor": "end", "class": "s-ylab"}, grid, mg);
    });
    var span = v[1] - v[0], every = span <= 200 ? 30 : span <= 420 ? 60 : 120;
    for (var t = Math.ceil(v[0] / every) * every; t <= v[1]; t += every) {
      make("text", {x: X(t), y: PY1 + 22, "text-anchor": "middle", "class": "s-xlab"}, grid, tick(t));
    }

    var bands = clear("s-bands");
    if (SC === "lights") band(st.ws, st.we, "soft", "Your active window", bands);
    if (SC === "window") band(st.ws, st.we, "strong", fmt(st.ws) + " \\u2013 " + fmt(st.we), bands);
    if (SC === "ondemand") band(st.ws, st.we, "soft", "Active window", bands);

    var th = clear("s-thresholds");
    if (SC === "lights") {
      threshold(st.yellow, "yellow", false, "High", th);
      threshold(st.red, "red", false, "Very high", th);
    } else if (SC === "levels") {
      threshold(st.yellow, "yellow", true, "Yellow above " + st.yellow, th);
      threshold(st.red, "red", true, "Red above " + st.red, th);
      threshold(70, "low", false, "Purple below 70", th);
    } else if (SC === "ramp" || SC === "ondemand") {
      threshold(yellowLine(), "yellow", false, "", th);
    }

    $("s-curve-full").setAttribute("d", pathFor(v[0], v[1]));
    var segs = SEGS = COLORED ? segments() : [];
    if (COLORED) drawColoredTrail(segs);

    var mk = clear("s-markers");
    if (SC === "lights") {
      // point out the white stretch: it's short on a whole-day chart
      var w = segs.filter(function (sg) { return sg.s === "white"; })[0];
      if (w) {
        var wx = X(w.a), wy = Y(glucose(w.a)), ly = Math.min(wy + 40, PY1 - 6);
        make("line", {x1: wx + 3, y1: wy + 8, x2: wx + 14, y2: ly - 13, "class": "s-leader"}, mk);
        keepInside(make("text", {x: wx + 10, y: ly, "class": "s-mklab"}, mk, "White: starting to climb"));
      }
    }
    if (SC === "ramp") {
      var yT = firstAbove(RAMP_YELLOW);
      ["high", "medium", "low"].forEach(function (level) {
        var tr = firstRamp(S.ramp_mg[level]);
        if (tr === null || (yT !== null && tr >= yT)) return;
        var sel = level === st.ramp;
        make("circle", {cx: X(tr), cy: Y(glucose(tr)), r: sel ? 7.5 : 4.5, "class": "s-mk" + (sel ? " sel" : "")}, mk);
      });
      if (st.ramp === "off") make("text", {x: PX0 + 10, y: PY0 + 14, "class": "s-note"}, mk, "Off: no white light \\u2014 the button waits for yellow");
    }
    if (SC === "ondemand") drawOnCall(false);
  }

  // ---- the button ----
  var LIGHTS = {
    off:    {a: "#F3F5F8", b: "#C9D1DB", glow: null,      ray: null,      say: "button is dark"},
    white:  {a: "#FFFFFF", b: "#FFFFFF", glow: "#FFFFFF", ray: "#2D2320", chart: "#8FA6CC", say: "button turns white: glucose is climbing"},
    yellow: {a: "#FFF0A6", b: "#F5C518", glow: "#F5C518", ray: "#E0A800", say: "button turns yellow: high"},
    red:    {a: "#FFB3AC", b: "#EB4438", glow: "#EB4438", ray: "#EB4438", say: "button turns red: very high"},
    purple: {a: "#F0C2FF", b: "#B44BE0", glow: "#B44BE0", ray: "#B44BE0", say: "button turns purple: low"},
    blue:   {a: "#BFD6FF", b: "#3B82F6", glow: "#3B82F6", ray: "#3B82F6", say: "two blue flashes"}
  };
  var legendRows = document.querySelectorAll(".legend li[data-light]");
  var lit = null;
  function setLight(name) {
    if (name === lit) return;
    lit = name;
    var L = LIGHTS[name], i;
    $("dome-a").setAttribute("stop-color", L.a);
    $("dome-b").setAttribute("stop-color", L.b);
    var stops = document.querySelectorAll(".pool-stop");
    for (i = 0; i < stops.length; i++) stops[i].setAttribute("stop-color", L.glow || "#fff");
    $("s-pool").setAttribute("opacity", L.glow ? 1 : 0);
    $("s-halo").setAttribute("fill", L.glow || "#fff");
    $("s-halo").setAttribute("opacity", L.glow ? (name === "white" ? 1 : 0.8) : 0);
    var rays = $("s-rays");
    rays.setAttribute("class", "s-rays" + (L.ray ? " on" : ""));
    if (L.ray) rays.setAttribute("stroke", L.ray);
    $("s-headdot").style.fill = L.glow ? L.b : "#fff";
    $("s-headglow").setAttribute("fill", L.chart || L.glow || "#fff");
    $("s-headglow").setAttribute("opacity", L.glow ? 0.75 : 0);
    $("cap-dot").style.background = L.glow ? L.b : "#fff";
    $("cap-dot").style.boxShadow = name === "white" ? "0 0 0 2px #fff, 0 0 6px 3px rgba(45, 35, 32, 0.3)" : "none";
    for (i = 0; i < legendRows.length; i++) legendRows[i].classList.toggle("on", legendRows[i].getAttribute("data-light") === name);
  }
  var lastCaption = "";
  function caption(t, name) {
    var text = fmt(t) + " \\u00b7 " + Math.round(glucose(t)) + " mg/dL \\u2192 " + LIGHTS[name].say;
    if (text !== lastCaption) { $("cap-text").textContent = text; lastCaption = text; }
  }
  function placeHead(t) {
    var v = view(), d = COLORED ? "" : pathFor(v[0], Math.max(v[0], t));
    $("s-curve-trail").setAttribute("d", d);
    $("clip-trail-r").setAttribute("width", COLORED ? Math.max(0, X(t)) : 0);
    var x = X(t), y = Y(glucose(t));
    $("s-headdot").setAttribute("cx", x);
    $("s-headdot").setAttribute("cy", y);
    $("s-headglow").setAttribute("cx", x);
    $("s-headglow").setAttribute("cy", y);
    $("s-headline").setAttribute("x1", x);
    $("s-headline").setAttribute("x2", x);
  }

  // ---- playback: a "now" dot sweeps across the day and the button follows it ----
  // The lights screen showcases each color in turn: the sweep pauses when the
  // button changes to a color, then takes FEATURE_MS to cross that stretch
  // (never faster than normal), so a short white rise gets as much time as a
  // long red one. Later screens sweep at one steady speed.
  var SWEEP_MS = 11000, FEATURE = SC === "lights", FEATURE_HOLD_MS = 650, FEATURE_MS = 1700;
  function segAt(t) {
    for (var i = 0; i < SEGS.length; i++) if (t >= SEGS[i].a && t < SEGS[i].b) return SEGS[i];
    return null;
  }
  var head = view()[0], prevHead = head, lastTs = null, holdUntil = 0, flashAt = null;
  function lightFor(t, now) {
    if (flashAt !== null) {
      var ph = Math.floor((now - flashAt) / 170);
      if (ph < 4) return ph % 2 ? "off" : "blue";
      flashAt = null;
    }
    return statusAt(t);
  }
  function frame(now) {
    if (lastTs === null) lastTs = now;
    var dt = Math.min(64, now - lastTs);
    lastTs = now;
    if (now >= holdUntil) {
      var v = view();
      if (head < v[0] || head > v[1]) head = v[0];
      var base = (v[1] - v[0]) / SWEEP_MS, speed = base, sg = FEATURE && lit !== "off" && segAt(head);
      if (sg) speed = Math.min(base, (sg.b - sg.a) / FEATURE_MS);
      prevHead = head;
      head += dt * speed;
      if (SC === "ondemand") {
        var a = st.ocStart, b = st.ocStart + st.onCall;
        if ((prevHead < a && head >= a) || (prevHead < b && head >= b && !inSched(b))) flashAt = now;
      }
      if (head >= v[1]) { head = v[1]; holdUntil = now + 1400; }
      placeHead(head);
      var name = lightFor(head, now);
      if (FEATURE && name !== lit && name !== "off") holdUntil = Math.max(holdUntil, now + FEATURE_HOLD_MS);
      setLight(name);
      caption(head, name);
      if (head >= v[1]) head = v[0] - 1;  // restart after the hold
    }
    requestAnimationFrame(frame);
  }

  function representativeTime() {
    var v = view();
    if (SC === "ramp") { var tr = st.ramp && S.ramp_mg[st.ramp] && firstRamp(S.ramp_mg[st.ramp]); if (tr) return tr; }
    if (SC === "ondemand") return Math.min(v[1], st.ocStart + st.onCall / 2);
    for (var t = v[0]; t <= v[1]; t += 5) if (statusAt(t) !== "off") return t;
    return v[1];
  }
  function showStill() {
    var t = representativeTime();
    placeHead(t);
    if (COLORED) $("clip-trail-r").setAttribute("width", 600);  // show the whole colored day
    setLight(statusAt(t));
    caption(t, statusAt(t));
  }

  function changed() { readInputs(); render(); if (reduced) showStill(); }
  ["window_start", "window_end", "yellow_threshold", "red_threshold"].forEach(function (id) {
    var e = $(id);
    if (e) { e.addEventListener("input", changed); e.addEventListener("change", changed); }
  });
  Array.prototype.forEach.call(document.querySelectorAll('input[name="ramp_sensitivity"]'), function (e) {
    e.addEventListener("change", function () { changed(); drawWhiteBox(true); });
  });
  Array.prototype.forEach.call(document.querySelectorAll('input[name="on_call_minutes"]'), function (e) {
    e.addEventListener("change", function () { readInputs(); render(); drawOnCall(true); if (reduced) showStill(); });
  });

  readInputs();
  render();
  if (SC === "ramp") drawWhiteBox(false);
  if (SC === "ondemand") drawOnCall(true);
  setLight("off");
  if (reduced) showStill(); else requestAnimationFrame(frame);
})();
</script>
{% endif %}
"""

STEP_TEXT = {
    1: ("Glucose awareness, right when you need it",
        "HyLight sits on your desk and watches your Dexcom for you. During the hours you choose, "
        "it lights up the moment your glucose starts to climb, so you can act early without "
        "checking your phone.",
        ""),
    2: ("What the light tells you",
        "After a meal, glucose can climb fast. HyLight turns white as soon as the rise begins, "
        "often well before you're high, so you can act early. If it keeps climbing, the light "
        "turns yellow, then red.",
        "A sample day, with the line drawn in the color the button shows: white as glucose "
        "starts to climb after lunch, then yellow and red as it crosses the high lines, only "
        "inside the active window."),
    3: ("When should HyLight watch?",
        "Choose your active window: the hours you want help staying on top of your glucose. "
        "Outside it, the light stays dark.",
        "The sample day with your active window shaded in blue."),
    4: ("Set your high levels",
        "During your active window, the button turns yellow above your first level and red "
        "above the second.",
        "The sample day with your yellow and red lines."),
    5: ("Catch the rise early",
        "Ramp detection turns the button white as soon as your glucose starts climbing fast, "
        "often before it reaches yellow, so you can act sooner.",
        "A lunchtime climb, with a white box over the stretch where the light is white before "
        "it turns yellow."),
    6: ("Need it at another time? Double-press.",
        "A double press turns HyLight on for a while, even outside your active window. Two "
        "blue flashes start it; two more mean time is up.",
        "An evening with an on-demand window shaded in blue."),
}

REGIONS = [("us", "United States"), ("ous", "Outside the US"), ("jp", "Japan")]


def register(app, cfg, controller, brand_head, brand_foot, connect_dexcom, cover_logo_b64):
    """connect_dexcom(form) logs in with the cover's form and starts
    monitoring, returning an error message or None once connected."""
    page = brand_head + SETUP_BODY + brand_foot

    def render(step, error=None, form=None):
        shown = cfg.as_dict()
        if form is not None:
            # Redisplay what was typed after a validation error, not the saved values.
            for key in ("window_start", "window_end", "yellow_threshold", "red_threshold", "ramp_sensitivity"):
                if key in form:
                    shown[key] = form[key]
            if "window_start" in form:
                shown["active_days"] = [int(d) for d in form.getlist("active_days") if d.isdigit()]
            if form.get("on_call_minutes", "").isdigit():
                shown["on_call_minutes"] = int(form["on_call_minutes"])
        saved = cfg.as_dict()
        scene = {
            "step": step,
            "scene": SCENES.get(step),
            "window_start": saved["window_start"],
            "window_end": saved["window_end"],
            "yellow": saved["yellow_threshold"],
            "red": saved["red_threshold"],
            "ramp": saved["ramp_sensitivity"],
            "on_call": saved["on_call_minutes"],
            "on_call_start": ON_DEMAND_EXAMPLE_START,
            "ramp_mg": RAMP_SENSITIVITY_MG_DL,
        }
        title, lede, scene_label = STEP_TEXT[step]
        tour = cfg.get_onboarding_done()
        connected = controller.is_running()
        next_label = {2: "Set it up", LAST_STEP: "Finish"}.get(step, "Next")
        return render_template_string(
            page,
            step=step,
            last_step=LAST_STEP,
            progress_labels=PROGRESS_LABELS,
            scene=SCENES.get(step),
            title=title,
            lede=lede,
            scene_label=scene_label,
            scene_json=json.dumps(scene).replace("</", "<\\/"),
            cfg=shown,
            days=list(enumerate(DAY_NAMES)),
            ramp_choices=RAMP_SENSITIVITY_MG_DL,
            ramp_blurbs=RAMP_BLURBS,
            on_call_choices=ON_CALL_DURATION_CHOICES_MINUTES,
            next_label=next_label,
            tour=tour,
            connected=connected,
            username=controller.dexcom_username if connected else (form or {}).get("username", ""),
            regions=REGIONS,
            region=(form or {}).get("region", "us"),
            cover_logo=cover_logo_b64,
            error=error,
        )

    @app.route("/setup/<int:step>", methods=["GET", "POST"])
    def setup(step):
        if step not in STEP_GROUPS:
            return redirect(url_for("setup", step=1))
        if request.method == "POST":
            if step == 1 and not controller.is_running():
                error = connect_dexcom(request.form)
                if error:
                    return render(1, error=error, form=request.form)
            values, error = parse_settings_form(request.form, STEP_GROUPS[step])
            if error:
                return render(step, error=error + ".", form=request.form)
            if values:
                cfg.update(**values)
                controller.notify_settings_changed()
            if step < LAST_STEP:
                return redirect(url_for("setup", step=step + 1))
            cfg.update(onboarding_done=True)
            return redirect(url_for("index", setup="done"))
        return render(step)

    @app.route("/setup/exit", methods=["POST"])
    def setup_exit():
        cfg.update(onboarding_done=True)
        return redirect(url_for("index"))
