/* Minimal dashboard client: device control + status polling (no framework). */
(function () {
  "use strict";

  async function control(device, action, value) {
    const body = { device: device, action: action };
    if (value !== undefined && value !== null) body.value = value;

    try {
      const res = await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(function () { return {}; });
      if (!res.ok) {
        window.alert(data.error || "Aksi gagal");
        return;
      }
      await poll();
    } catch (err) {
      window.alert("Tidak dapat menghubungi perangkat");
    }
  }

  function bindDevice(card) {
    const name = card.dataset.device;
    card.querySelectorAll("button[data-action]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        control(name, btn.dataset.action);
      });
    });

    const slider = card.querySelector(".brightness");
    if (slider) {
      slider.addEventListener("change", function () {
        control(name, "set", Number(slider.value));
      });
    }
  }

  function setText(el, text) {
    if (el) el.textContent = text;
  }

  async function poll() {
    try {
      const res = await fetch("/api/status", { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      const data = await res.json();

      if (data.sensor) {
        setText(document.getElementById("sensor-temp"), Number(data.sensor.temp_c).toFixed(1));
        setText(document.getElementById("sensor-updated"), data.sensor.updated_at || "-");
      }
      setText(document.getElementById("uptime"), data.uptime_s);

      document.querySelectorAll(".device").forEach(function (card) {
        const dev = (data.devices || []).find(function (d) {
          return d.name === card.dataset.device;
        });
        if (!dev) return;

        const stateEl = card.querySelector(".state");
        if (stateEl) {
          stateEl.textContent = dev.state;
          stateEl.className = "state state-" + dev.state;
        }

        const valueEl = card.querySelector(".value");
        if (valueEl) valueEl.textContent = Math.round(dev.value);

        const slider = card.querySelector(".brightness");
        if (slider && document.activeElement !== slider) {
          slider.value = Math.round(dev.value);
        }
      });
    } catch (err) {
      /* device offline; keep last known state */
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".device").forEach(bindDevice);
    if (document.querySelector(".device")) {
      poll();
      window.setInterval(poll, 5000);
    }
  });
})();
