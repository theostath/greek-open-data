/*
  Progressive enhancement only. Every page renders and every answer is readable without this
  file; it adds the chart, the example-question shortcut, dataset pinning from the browse
  pages, copy-citation, the directory filter and the pre-flight health warning.
*/
(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function field() { return document.getElementById("question"); }

  /* ---- Example questions and rephrase -----------------------------------
     An example fills the ask field rather than submitting, so the user reads what they are
     about to ask and can edit it first. Rephrase selects the text, so the click visibly does
     something even when the field already holds the question. */
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest(".example, .rephrase");
    if (!trigger) return;
    var input = field();
    if (!input) return;
    input.value = trigger.dataset.question || input.value;
    input.focus();
    if (trigger.classList.contains("rephrase")) {
      input.select();
    } else {
      input.setSelectionRange(input.value.length, input.value.length);
    }
  });

  /* ---- Pinning a dataset from the browse list ----------------------------
     Captured before htmx sees the submit: instead of asking the dataset's title blind, the
     dataset is pinned into the ask form at the top, its title becomes an editable question,
     and the answer lands where the eye already is — not 6,000px above the clicked row. */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.classList || !form.classList.contains("pin-form")) return;
    var input = field();
    var pinnedId = document.getElementById("resource_id");
    if (!input || !pinnedId) return; // no ask form on this page: let htmx post it as-is
    event.preventDefault();
    event.stopPropagation();
    var title = form.querySelector("[name=question]").value;
    pin(form.querySelector("[name=resource_id]").value, title);
    input.value = title;
    var ask = document.getElementById("ask");
    ask.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
    input.focus({ preventScroll: true });
    input.select();
  }, true);

  function pin(resourceId, title) {
    var holder = document.getElementById("pinned");
    var pinnedId = document.getElementById("resource_id");
    if (!holder || !pinnedId) return;
    pinnedId.value = resourceId || "";
    document.getElementById("pinned-title").textContent = title || "";
    holder.hidden = !resourceId;
  }

  document.addEventListener("click", function (event) {
    if (!event.target.closest("#unpin")) return;
    pin("", "");
    var input = field();
    if (input) input.focus();
  });

  /* ---- Copy citation -----------------------------------------------------
     The citation string is assembled server-side from the whitelisted footer fields only;
     this just puts it on the clipboard. Hidden in the markup, shown only where it can work. */
  function revealCopy(root) {
    if (!navigator.clipboard) return;
    (root || document).querySelectorAll(".action-copy[hidden]").forEach(function (button) {
      button.hidden = false;
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest(".action-copy");
    if (!button) return;
    var status = button.parentElement.querySelector(".copy-status");
    navigator.clipboard.writeText(button.dataset.copy || "").then(function () {
      if (status) status.textContent = "Citation copied";
      button.classList.add("is-done");
      window.setTimeout(function () {
        if (status) status.textContent = "";
        button.classList.remove("is-done");
      }, 2400);
    }, function () {
      if (status) status.textContent = "Could not copy — select the source above instead";
    });
  });

  /* ---- Directory filter on /explore -------------------------------------- */
  function setupFilter() {
    var box = document.getElementById("filter");
    if (!box) return;
    box.hidden = false;
    var input = document.getElementById("filter-input");
    var count = document.getElementById("filter-count");
    var groups = document.querySelectorAll(".explore-group");
    // Accent- and case-insensitive, so "χανιων" finds "Δήμος Χανίων".
    function fold(text) {
      return text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
    }
    input.addEventListener("input", function () {
      var query = fold(input.value);
      var shown = 0;
      groups.forEach(function (group) {
        var visible = 0;
        group.querySelectorAll(".directory li").forEach(function (item) {
          var hit = !query || fold(item.textContent).indexOf(query) !== -1;
          item.hidden = !hit;
          if (hit) visible += 1;
        });
        group.hidden = visible === 0;
        shown += visible;
      });
      count.textContent = query
        ? (shown === 0 ? "Nothing matches." : shown + (shown === 1 ? " match" : " matches"))
        : "";
    });
  }

  /* ---- Charts -----------------------------------------------------------
     The option arrives as application/json, never as executable code, which is what lets the
     CSP keep script-src at 'self' with no 'unsafe-inline' — and means no ECharts option can
     smuggle in a JavaScript formatter. */
  var charts = [];

  function renderCharts(root) {
    var figures = (root || document).querySelectorAll(".chart");
    figures.forEach(function (figure) {
      var holder = figure.querySelector(".chart-option");
      var target = figure.querySelector(".chart-target");
      if (!holder || !target || target.dataset.rendered) return;
      if (!window.echarts) return;

      var option;
      try {
        option = JSON.parse(holder.textContent);
      } catch (err) {
        return; // every figure is in the table above; a broken chart is not fatal
      }

      target.dataset.rendered = "1";
      try {
        var chart = window.echarts.init(target, null, { renderer: "canvas" });
        // Animation is already off in the option; this is belt and braces for the case where
        // a future option forgets, since a reduced-motion user must not be animated at.
        chart.setOption(option, { notMerge: true, silent: reduced });
        // Presentation only, merged over the validated option: the figcaption above the plot
        // already carries the title and caveat, so the canvas copy is hidden rather than
        // printed twice, and the plot reclaims the space it held. No value is touched.
        if (option.title) {
          chart.setOption({ title: { show: false }, grid: { top: 28 } });
        }
        // On a narrow column ECharts drops overlapping category labels, so on a phone a bar
        // could lose its name. A short category axis keeps every label, angled to fit.
        var axis = option.xAxis;
        if (axis && !Array.isArray(axis) && axis.type === "category" &&
            Array.isArray(axis.data) && axis.data.length <= 8 && target.clientWidth < 520) {
          chart.setOption({
            xAxis: { axisLabel: { interval: 0, rotate: 35, hideOverlap: false }, nameGap: 64 },
            grid: { bottom: 48 },
          });
        }
        charts.push(chart);
      } catch (err) {
        target.remove();
      }
    });
  }

  // ECharts sizes to its container at init and does not observe resizes itself.
  window.addEventListener("resize", function () {
    charts.forEach(function (chart) {
      try { chart.resize(); } catch (err) { /* a disposed chart is not an error */ }
    });
  });

  document.addEventListener("DOMContentLoaded", function () {
    renderCharts(document);
    revealCopy(document);
    setupFilter();
    checkHealth();
  });

  // HTMX swaps the result in without a page load, so charts are rendered again after each.
  document.body.addEventListener("htmx:afterSwap", function (event) {
    renderCharts(event.target);
    revealCopy(event.target);
    // A finished result that landed out of view is brought to the reader, once. Progress
    // fragments are swapped every second and must never move the page.
    var result = event.target.querySelector && event.target.querySelector(".result");
    if (result) {
      var box = result.getBoundingClientRect();
      if (box.top < 0 || box.top > window.innerHeight * 0.8) {
        result.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
      }
    }
  });

  /* ---- Pre-flight health -------------------------------------------------
     Warns before the user types, rather than after they have waited two minutes. */
  function checkHealth() {
    var notice = document.getElementById("health");
    if (!notice) return;
    fetch("/healthz")
      .then(function (response) { return response.json(); })
      .then(function (health) {
        var problems = [];
        if (!health.llm_reachable) {
          problems.push(
            "the language model at Ollama is not responding, so questions cannot be planned"
          );
        }
        if (!health.datasets) {
          problems.push("the catalogue is empty — run the harvest first");
        } else if (!health.dense_index || !health.lexical_index) {
          problems.push("the search index is missing — run the index build first");
        }
        if (!problems.length) return;
        document.getElementById("health-detail").textContent =
          problems.join("; ") + ".";
        notice.hidden = false;
      })
      .catch(function () { /* health is advisory; its absence is not itself an error */ });
  }
})();
