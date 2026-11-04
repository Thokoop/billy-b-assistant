// ===================== RINGTONES (Bluetooth settings) =====================
// Works like the wake-up sounds: rows to generate (current persona's voice),
// upload, play and remove. The inputs have no name, so they are not part of
// the settings form's draft; every change is saved straight away.
window.Ringtones = (() => {
    const escapeHtml = (value) => String(value ?? "")
        .replaceAll("&", "&amp;").replaceAll('"', "&quot;")
        .replaceAll("<", "&lt;").replaceAll(">", "&gt;");

    const list = () => document.getElementById("ringtone-list");

    function rowHtml(tone) {
        const builtIn = tone.kind === "default";
        // A new tone shows its placeholder; an uploaded one its file name.
        const value = builtIn ? tone.name : (tone.phrase || (tone.has_audio ? tone.name : ""));
        const placeholder = "What should the ringtone say? (e.g. Ring ring!)";
        // A list row (Design System 3.1) with icon buttons (3.2).
        const button = (cls, hue, icon, label) => `
                    <button type="button" class="${cls} secondary-action secondary-action--hover--${hue} button-icon shrink-0"
                            title="${label}" aria-label="${label}">
                        <span class="material-icons" aria-hidden="true">${icon}</span>
                    </button>`;
        return `
            <div class="p-3 bg-zinc-800/50 rounded-lg border border-zinc-700" data-ringtone="${escapeHtml(tone.id)}">
                <div class="flex items-center gap-2">
                    <input type="text" class="ringtone-text field flex-1 min-w-0" value="${escapeHtml(value)}"
                           placeholder="${escapeHtml(placeholder)}" ${builtIn ? "readonly" : ""}
                           aria-label="${builtIn ? "Built-in ringtone" : "Ringtone text"}">
                    ${builtIn
                        ? `<span class="w-11 shrink-0" aria-hidden="true"></span>`
                        : button("ringtone-generate", "cyan", "auto_fix_high", "Generate in the current persona's voice")}
                    ${button("ringtone-upload", "cyan", "upload_file", "Upload your own audio file")}
                    ${tone.has_audio
                        ? button("ringtone-play", "emerald", "play_arrow", "Play")
                        : `<span class="w-11 shrink-0" aria-hidden="true"></span>`}
                    ${button("ringtone-remove", "rose", "delete", "Remove")}
                </div>
                ${!builtIn && !tone.has_audio ? `<p class="text-xs text-amber-300 mt-1">No audio yet: generate or upload it.</p>` : ""}
            </div>`;
    }

    async function load() {
        const container = list();
        if (!container) return;
        try {
            const data = await requestJson("/ringtones", {error: "Could not load the ringtones"});
            const note = document.getElementById("ringtone-telephony-note");
            if (note) note.hidden = data.telephony !== false;
            const tones = data.ringtones || [];
            container.innerHTML = tones.length
                ? tones.map(rowHtml).join("")
                : `<div class="text-sm text-zinc-400 italic py-2">No ringtones. Billy stays silent while your phone rings; add one, or restore the defaults.</div>`;
        } catch (err) {
            showNotification(err.message, "error");
        }
    }

    const post = (url, body, error) => requestJson(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body || {}),
        error,
    });

    async function busy(button, work) {
        const icon = button.querySelector(".material-icons, i");
        const previous = icon?.textContent;
        button.disabled = true;
        button.classList.add("opacity-50");
        if (icon) icon.textContent = "hourglass_empty";
        try {
            await work();
        } catch (err) {
            showNotification(err.message, "error");
        } finally {
            button.disabled = false;
            button.classList.remove("opacity-50");
            if (icon) icon.textContent = previous;
        }
    }

    function upload(id, button) {
        const input = document.createElement("input");
        input.type = "file";
        input.accept = "audio/*,.mp3,.m4a,.wav,.ogg,.flac,.aac";
        input.addEventListener("change", () => {
            const file = input.files?.[0];
            if (!file) return;
            busy(button, async () => {
                const form = new FormData();
                form.append("file", file);
                await requestJson(`/ringtones/${encodeURIComponent(id)}/upload`, {
                    method: "POST", body: form, error: "Upload failed",
                });
                showNotification(`Ringtone "${file.name}" saved`, "success");
                await load();
            });
        });
        input.click();
    }

    // The list only matters when Billy plays his own ringtones.
    function syncChoice() {
        const choice = document.getElementById("BLUETOOTH_RINGTONE");
        const options = document.getElementById("ringtone-billy-options");
        if (choice && options) options.hidden = choice.value !== "billy";
    }

    function bind() {
        const container = list();
        if (!container) return;
        load();
        syncChoice();
        const choice = document.getElementById("BLUETOOTH_RINGTONE");
        if (choice && choice.dataset.ringtoneBound !== "true") {
            choice.dataset.ringtoneBound = "true";
            choice.addEventListener("change", syncChoice);
        }
        if (container.dataset.bound === "true") return;
        container.dataset.bound = "true";

        container.addEventListener("click", (event) => {
            const row = event.target.closest("[data-ringtone]");
            const button = event.target.closest("button");
            if (!row || !button) return;
            const id = row.dataset.ringtone;
            const url = `/ringtones/${encodeURIComponent(id)}`;
            const text = row.querySelector(".ringtone-text")?.value.trim() || "";

            if (button.classList.contains("ringtone-play")) {
                busy(button, async () => {
                    const data = await post(`${url}/play`, {}, "Could not play the ringtone");
                    showNotification(data.status, "success");
                });
            } else if (button.classList.contains("ringtone-generate")) {
                if (!text) {
                    showNotification("Enter what the ringtone should say first", "warning");
                    return;
                }
                busy(button, async () => {
                    await post(`${url}/generate`, {phrase: text}, "Generating failed");
                    showNotification("Ringtone generated. Press Play to hear it.", "success");
                    await load();
                });
            } else if (button.classList.contains("ringtone-upload")) {
                upload(id, button);
            } else if (button.classList.contains("ringtone-remove")) {
                if (!confirm("Remove this ringtone?")) return;
                busy(button, async () => {
                    await post(`${url}/remove`, {}, "Could not remove the ringtone");
                    await load();
                });
            }
        });

        // Typed text is kept for the next Generate, also after a reload.
        container.addEventListener("change", (event) => {
            const input = event.target.closest(".ringtone-text");
            const row = event.target.closest("[data-ringtone]");
            if (!input || !row || input.readOnly) return;
            post(`/ringtones/${encodeURIComponent(row.dataset.ringtone)}/update`,
                {phrase: input.value.trim(), name: input.value.trim()}, "Could not save the ringtone")
                .catch((err) => showNotification(err.message, "error"));
        });

        document.getElementById("ringtone-add")?.addEventListener("click", async () => {
            try {
                await post("/ringtones/add", {}, "Could not add a ringtone");
                await load();
                list()?.querySelector("[data-ringtone]:last-child .ringtone-text")?.focus();
            } catch (err) {
                showNotification(err.message, "error");
            }
        });

        const ideas = document.getElementById("ringtone-ideas");
        ideas?.addEventListener("click", () => busy(ideas, async () => {
            const data = await post("/ringtones/ideas", {count: 5}, "Could not come up with ringtones");
            const phrases = data.ideas || [];
            for (const phrase of phrases) {
                await post("/ringtones/add", {phrase}, "Could not add a ringtone");
            }
            await load();
            showNotification(`Added ${phrases.length} ringtone ideas. Press Generate on each to record it.`, "success");
        }));

        const restore = document.getElementById("ringtone-restore");
        restore?.addEventListener("click", () => busy(restore, async () => {
            const data = await post("/ringtones/restore", {}, "Could not restore the ringtones");
            showNotification(data.restored
                ? `Restored ${data.restored} built-in ringtone${data.restored === 1 ? "" : "s"}`
                : "All built-in ringtones are already in the list", data.restored ? "success" : "info");
            await load();
        }));
    }

    return {bind, load};
})();
