// ===================== MOTOR TEST PANEL =====================
const MotorPanel = (() => {
    function sendMotorTest(motor) {
        fetch("/test-motor", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({motor})
        })
            .then(res => res.json())
            .then(data => {
                if (data.error) {
                    showNotification("Motor error: " + data.error, "error", 4000);
                } else {
                    showNotification(`Tested ${motor}`, "success", 1500);
                    if (data.service_was_active) {
                        showNotification(
                            "Billy was stopped for hardware test. Please restart Billy again when done.",
                            "warning",
                            7000
                        );
                        ServiceStatus.fetchStatus();
                    }
                }
            })
            .catch(err => showNotification("Motor test failed: " + err, "error"));
    }

    function sendLedTest() {
        fetch("/test-led", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({})
        })
            .then(res => res.json().then(data => ({ok: res.ok, data})))
            .then(({ok, data}) => {
                if (!ok || data.error) {
                    showNotification("LED test failed: " + (data.error || "Unknown error"), "error", 4000);
                    return;
                }
                showNotification("Status LED test completed", "success", 1500);
                if (data.service_was_active) {
                    showNotification(
                        "Billy was stopped for hardware test. Please restart Billy again when done.",
                        "warning",
                        7000
                    );
                    ServiceStatus.fetchStatus();
                }
            })
            .catch(err => showNotification("LED test failed: " + err, "error"));
    }

    function bindUI() {
        ["mouth", "head", "tail"].forEach(motor => {
            const btn = document.getElementById(`test-${motor}-btn`);
            if (btn) {
                btn.addEventListener("click", function () { sendMotorTest(motor); });
            }
        });

        const ledBtn = document.getElementById("test-led-btn");
        if (ledBtn) {
            ledBtn.addEventListener("click", sendLedTest);
        }
    }

    return {bindUI};
})();

