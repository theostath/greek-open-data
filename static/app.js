/*
  Progressive enhancement only. Every page renders and every answer is readable without this
  file; it adds the chart, the example-question shortcut and the pre-flight health warning.
*/
(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- Example questions and re-ask -------------------------------------
     Fills the ask field rather than submitting, so the user reads what they are about to ask
     and can edit it first. */
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest(".example");
    if (!trigger) return;
    var field = document.getElementById("question");
    if (!field) return;
    field.value = trigger.dataset.question || "";
    field.focus();
    field.setSelectionRange(field.value.length, field.value.length);
  });

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
    checkHealth();
  });

  // HTMX swaps the result in without a page load, so charts are rendered again after each.
  document.body.addEventListener("htmx:afterSwap", function (event) {
    renderCharts(event.target);
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
