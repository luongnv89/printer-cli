// printer-cli examples: one slide at a time, advancing on its own.
// Autoplay runs only while nothing pauses it. userPaused is the visitor's choice: any manual navigation sets
// it, and only the Autoplay button (or P / Space) clears it. Hover, focus inside the slide and a hidden tab
// pause it only for as long as they last (WCAG 2.2.2).
(() => {
  "use strict";

  const DURATION = 7000; // ms per slide: time to read the title, the command and the prompt
  const SWIPE = 48; // px of horizontal travel that counts as a swipe

  const slides = JSON.parse(document.getElementById("slides").textContent);
  const byId = (id) => document.getElementById(id);
  const stage = byId("stage");
  const dots = byId("dots");
  const progress = byId("progress");
  const toggle = byId("autoplay");
  const announce = byId("announce");
  const copyStatus = byId("copy-status");
  const current = byId("current");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  let index = 0;
  let userPaused = reduceMotion.matches;
  let hoverPaused = false;
  let focusPaused = false;
  let hidden = document.hidden;
  let elapsed = 0;
  let last = null;

  const pad = (n) => String(n).padStart(2, "0");

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function img(src, alt, width, height) {
    const node = el("img");
    node.src = src;
    node.alt = alt;
    if (width) node.width = width;
    if (height) node.height = height;
    node.decoding = "async";
    return node;
  }

  // Each command line behind a "$ " prompt glyph that is shown but never copied; long lines wrap under it.
  function commandLines(command) {
    const pre = el("pre");
    command.split("\n").forEach((line) => {
      const row = el("span", "line");
      const glyph = el("span", "ps", "$ ");
      glyph.setAttribute("aria-hidden", "true");
      row.append(glyph, line);
      pre.append(row);
    });
    return pre;
  }

  // A sheet of commands with no output: each "# comment" goes on its own line, so the sheet stays narrow.
  function transcript(command) {
    return command.split("\n").map((line) => {
      const [, cmd, note] = line.match(/^(.*?\S)\s{2,}(#.*)$/) || [null, line, ""];
      return note ? `$ ${cmd}\n    ${note}` : `$ ${cmd}`;
    }).join("\n");
  }

  function selectText(node) {
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function copyButton(what, text, source) {
    const button = el("button", "copy", "Copy");
    button.type = "button";
    button.setAttribute("aria-label", `Copy the ${what}`);
    let timer;
    button.addEventListener("click", async () => {
      let shown;
      try {
        await navigator.clipboard.writeText(text);
        shown = "Copied";
        copyStatus.textContent = `Copied the ${what}.`;
      } catch {
        // No clipboard access (an old browser or a blocked permission): select the text for a manual copy.
        selectText(source);
        shown = "Selected";
        copyStatus.textContent = `The ${what} is selected: copy it with your keyboard.`;
      }
      button.textContent = shown;
      clearTimeout(timer);
      timer = setTimeout(() => { button.textContent = "Copy"; }, 2000);
    });
    return button;
  }

  function box(className, label, body, what, text) {
    const section = el("section", `box ${className}`);
    const head = el("div", "box-head");
    head.append(el("span", "label", label), copyButton(what, text, body));
    section.setAttribute("aria-label", label);
    section.append(head, body);
    return section;
  }

  function matFor(slide) {
    const mat = el("div", "mat");
    if (slide.kind === "image") {
      mat.append(Object.assign(img(slide.image, slide.alt, slide.width, slide.height), { className: "proof" }));
    } else if (slide.kind === "terminal") {
      const text = slide.output || transcript(slide.command);
      const sheet = el("div", "sheet");
      sheet.tabIndex = 0; // the sheet scrolls when its type cannot shrink enough, so it must take focus
      sheet.setAttribute("role", "region");
      sheet.setAttribute("aria-label", slide.output ? "What the command prints" : "The commands");
      sheet.style.setProperty("--cols", Math.max(...text.split("\n").map((line) => line.length)));
      sheet.append(el("pre", null, text));
      mat.append(sheet);
    } else {
      const stack = el("div", "stack");
      slide.stack.forEach((src) => stack.append(img(src, "", 800, 1132)));
      mat.append(stack);
    }
    return mat;
  }

  function render() {
    const slide = slides[index];
    const article = el("article", `slide slide-${slide.kind}`);
    article.setAttribute("aria-roledescription", "slide");
    article.setAttribute("aria-label", `${index + 1} of ${slides.length}: ${slide.title}`);

    const inner = el("div", "panel-inner");
    const prompt = el("p", null, slide.prompt);
    const command = commandLines(slide.command);
    inner.append(
      el("p", "group", slide.group),
      el("h2", "title", slide.title),
      el("p", "blurb", slide.blurb),
      box("cmd", "Command", command, "command", slide.command),
      box("ask", "Ask your agent", prompt, "prompt", slide.prompt),
    );
    const panel = el("div", "panel");
    panel.append(inner);
    article.append(matFor(slide), panel);
    stage.replaceChildren(article);
    // The focused copy button may just have been replaced; only real focus inside the slide pauses autoplay.
    focusPaused = stage.contains(document.activeElement);

    const next = slides[(index + 1) % slides.length];
    if (next.image) img(next.image, "");
  }

  function sync() {
    toggle.setAttribute("aria-pressed", String(!userPaused));
    current.textContent = pad(index + 1);
    dots.querySelectorAll(".dot").forEach((dot, i) => {
      if (i === index) dot.setAttribute("aria-current", "true");
      else dot.removeAttribute("aria-current");
    });
    const active = dots.children[index];
    dots.scrollTo({
      left: active.offsetLeft - (dots.clientWidth - active.offsetWidth) / 2,
      behavior: reduceMotion.matches ? "auto" : "smooth",
    });
  }

  function go(to, manual) {
    index = (to + slides.length) % slides.length;
    elapsed = 0;
    if (manual) {
      userPaused = true;
      announce.textContent = `Slide ${index + 1} of ${slides.length}: ${slides[index].title}`;
    }
    render();
    sync();
  }

  function toggleAutoplay() {
    userPaused = !userPaused;
    sync();
  }

  const playing = () => !(userPaused || hoverPaused || focusPaused || hidden);

  function tick(now) {
    if (playing()) {
      if (last !== null) elapsed += now - last;
      last = now;
      if (elapsed >= DURATION) go(index + 1, false);
    } else {
      last = null;
    }
    progress.style.transform = `scaleX(${Math.min(elapsed / DURATION, 1)})`;
    requestAnimationFrame(tick);
  }

  slides.forEach((slide, i) => {
    const dot = el("button", "dot");
    dot.type = "button";
    dot.setAttribute("aria-label", `Go to slide ${i + 1}: ${slide.title}`);
    if (i > 0 && slide.group !== slides[i - 1].group) dot.classList.add("section");
    dot.addEventListener("click", () => go(i, true));
    dots.append(dot);
  });
  byId("total").textContent = pad(slides.length);

  byId("prev").addEventListener("click", () => go(index - 1, true));
  byId("next").addEventListener("click", () => go(index + 1, true));
  toggle.addEventListener("click", toggleAutoplay);

  document.addEventListener("keydown", (event) => {
    if (event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target instanceof Element ? event.target : document.body;
    const key = event.key;
    if (key === "p" || key === "P") {
      if (target.closest("input, textarea, select, [contenteditable]")) return;
      event.preventDefault();
      toggleAutoplay();
    } else if (key === " ") {
      // On a control, Space keeps its own meaning (press the button, scroll the sheet) and toggles nothing.
      if (target.closest("button, a, input, select, textarea, summary, [contenteditable], [tabindex]")) return;
      event.preventDefault();
      toggleAutoplay();
    } else if (key === "ArrowLeft" || key === "ArrowRight") {
      if (target.closest(".sheet, input, textarea, select")) return;
      event.preventDefault();
      go(index + (key === "ArrowRight" ? 1 : -1), true);
    }
  });

  // Hover pauses for a mouse only: a touch fires an enter with no matching leave.
  stage.addEventListener("pointerenter", (event) => { if (event.pointerType === "mouse") hoverPaused = true; });
  stage.addEventListener("pointerleave", (event) => { if (event.pointerType === "mouse") hoverPaused = false; });
  stage.addEventListener("focusin", () => { focusPaused = true; });
  stage.addEventListener("focusout", (event) => { focusPaused = stage.contains(event.relatedTarget); });
  document.addEventListener("visibilitychange", () => { hidden = document.hidden; });
  reduceMotion.addEventListener("change", () => {
    if (reduceMotion.matches) userPaused = true;
    sync();
  });

  let touch = null;
  stage.addEventListener("touchstart", (event) => {
    touch = event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null;
  }, { passive: true });
  stage.addEventListener("touchend", (event) => {
    if (!touch) return;
    const dx = event.changedTouches[0].clientX - touch.x;
    const dy = event.changedTouches[0].clientY - touch.y;
    touch = null;
    if (Math.abs(dx) > SWIPE && Math.abs(dx) > 1.5 * Math.abs(dy)) go(index + (dx < 0 ? 1 : -1), true);
  }, { passive: true });

  render();
  sync();
  requestAnimationFrame(tick);
})();
