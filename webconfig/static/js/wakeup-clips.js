// ===================== WAKEUP CLIPS =====================
const WAKEUP_MOOD_PRESETS = [
    "neutral",
    "calm",
    "cheerful",
    "warm",
    "curious",
    "focused",
    "playful",
    "mischievous",
    "excited",
    "surprised",
    "sleepy",
    "bored",
    "sad",
    "anxious",
    "flustered",
    "annoyed",
    "grumpy",
    "dramatic",
];

function escapeWakeupHtml(value) {
    return String(value || "")
        .replaceAll("&", "&amp;")
        .replaceAll('"', "&quot;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
}

function getCurrentPersonaName(personaName = null) {
    if (personaName) return personaName;
    const selectedRow = document.querySelector('#persona-list [data-persona].border-emerald-500');
    return selectedRow && selectedRow.getAttribute('data-persona') || 'default';
}

function renderWakeupMoodPills(selectedMoods = []) {
    const selected = new Set((selectedMoods || []).map((mood) => String(mood).toLowerCase()));
    return WAKEUP_MOOD_PRESETS.map((mood) => (
        `<button type="button"
                 class="wakeup-mood-pill secondary-action secondary-action--hover--cyan text-xs px-3 py-1.5 ${selected.has(mood) ? 'secondary-action--active-cyan' : ''}"
                 data-mood="${mood}"
                 aria-pressed="${selected.has(mood) ? 'true' : 'false'}">
            ${mood}
        </button>`
    )).join("");
}

function renderWakeupMoodSummary(selectedMoods = []) {
    const moods = selectedMoods || [];
    if (moods.length === 0) return "All moods";
    if (moods.length <= 2) return moods.join(", ");
    return `${moods.slice(0, 2).join(", ")} +${moods.length - 2}`;
}

function renderWakeupMoodBadge(selectedMoods = []) {
    const count = (selectedMoods || []).length;
    return count > 0 ? String(count) : "";
}

function renderWakeupRow(index, phrase = "", moods = [], hasAudio = false) {
    const moodSummary = renderWakeupMoodSummary(moods);
    return `
        <div class="flex flex-col gap-2 rounded-lg border border-zinc-700 bg-zinc-900/40 p-2" data-index="${index}">
            <div class="flex items-center gap-2">
                <input type="text" class="wakeup-phrase-input text-input w-full rounded bg-zinc-800 border border-zinc-700 p-2" value="${escapeWakeupHtml(phrase)}" placeholder="word or phrase">
                <button type="button" class="wakeup-mood-toggle wakeup-action-button secondary-action secondary-action--hover--cyan h-11 shrink-0 relative" title="Edit mood tags: ${escapeWakeupHtml(moodSummary)}" aria-label="Edit moods">
                    <i class="material-icons text-[20px] leading-none">mood</i>
                    <span class="wakeup-action-label">Moods</span>
                    <span class="wakeup-mood-summary absolute -top-1 -right-1 min-w-4 h-4 px-1 rounded-full bg-cyan-500 text-[10px] leading-4 text-zinc-950 ${moods.length ? '' : 'hidden'}">${escapeWakeupHtml(renderWakeupMoodBadge(moods))}</span>
                </button>
                <button type="button" class="wakeup-generate-btn wakeup-action-button secondary-action secondary-action--hover--amber h-11 shrink-0" title="Generate .wav" aria-label="Generate audio">
                    <i class="material-icons text-[22px] leading-none">auto_fix_high</i>
                    <span class="wakeup-action-label">Generate</span>
                </button>
                <button type="button" class="wakeup-play-btn wakeup-action-button secondary-action secondary-action--hover--emerald h-11 shrink-0 ${!hasAudio ? 'invisible' : ''}" title="Play .wav" aria-label="Play audio">
                    <i class="material-icons text-[22px] leading-none">play_arrow</i>
                    <span class="wakeup-action-label">Play</span>
                </button>
                <button type="button" class="remove-wakeup-row wakeup-action-button secondary-action secondary-action--hover--rose h-11 shrink-0" title="Remove" aria-label="Remove wake-up phrase">
                    <i class="material-icons text-[22px] leading-none">delete</i>
                    <span class="wakeup-action-label">Remove</span>
                </button>
            </div>
            <div class="wakeup-mood-panel hidden">
                <div class="wakeup-mood-pills flex flex-wrap gap-2 pt-1">
                    ${renderWakeupMoodPills(moods)}
                </div>
            </div>
        </div>
    `;
}

function createWakeupRow(index, phrase = "", moods = [], hasAudio = false) {
    const template = document.createElement("template");
    template.innerHTML = renderWakeupRow(index, phrase, moods, hasAudio).trim();
    return template.content.firstElementChild;
}

function getWakeupRowMoods(row) {
    return Array.from(row.querySelectorAll(".wakeup-mood-pill[aria-pressed='true']"))
        .map((button) => button.dataset.mood)
        .filter(Boolean);
}

function updateWakeupMoodSummary(row) {
    const moods = getWakeupRowMoods(row);
    const button = row.querySelector(".wakeup-mood-toggle");
    const summary = row.querySelector(".wakeup-mood-summary");
    if (button) {
        button.title = `Edit mood tags: ${renderWakeupMoodSummary(moods)}`;
    }
    if (summary) {
        summary.textContent = renderWakeupMoodBadge(moods);
        summary.classList.toggle("hidden", moods.length === 0);
    }
}

function collectWakeupRows() {
    const wakeup = {};
    const rows = document.querySelectorAll("#wakeup-sound-list [data-index]");
    let currentIndex = 1;
    rows.forEach((row) => {
        const phrase = row.querySelector(".wakeup-phrase-input")?.value?.trim() || "";
        if (!phrase) return;
        wakeup[currentIndex++] = {
            text: phrase,
            moods: getWakeupRowMoods(row),
        };
    });
    return wakeup;
}

async function saveWakeupPhrase(index, phrase, moods = [], personaName = null) {
    const resPersona = await fetch("/persona/wakeup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            index: String(index),
            phrase: phrase,
            moods: moods,
            persona: getCurrentPersonaName(personaName),
        }),
    });
    if (!resPersona.ok) {
        const err = await resPersona.json();
        throw new Error(err.error || "Failed to update persona");
    }
}

async function loadWakeupClips(personaName = null) {
    const container = document.getElementById("wakeup-sound-list");
    if (!container) return;
    container.innerHTML = "";
    try {
        const persona = encodeURIComponent(getCurrentPersonaName(personaName));
        const res = await fetch(`/wakeup?persona=${persona}`);
        const { clips } = await res.json();
        if (clips.length === 0) {
            const message = document.createElement("div");
            message.className = "text-sm text-zinc-400 italic py-2";
            message.textContent = "No custom wake-up clips added. Using the default sounds.";
            container.appendChild(message);
            return;
        } else {
            const label = document.createElement("label");
            label.className = "flex items-center justify-between text-sm text-slate-300 mb-1";
            label.textContent = "Words or phrases that Billy will randomly say on activation:";
            container.appendChild(label);
        }
        clips.sort((a, b) => a.index - b.index).forEach(({ index, phrase, moods, has_audio }) => {
            const row = createWakeupRow(index, phrase, moods, has_audio);
            container.appendChild(row);
        });
    } catch (err) {
        console.error("Failed to load wakeup clips:", err);
        showNotification("Failed to load wakeup clips", "error");
    }
}

function addWakeupSound(index = null, phrase = "", hasAudio = false, moods = []) {
    const container = document.getElementById("wakeup-sound-list");
    if (!container) return null;
    const rows = container.querySelectorAll("div[data-index]");
    const usedIndices = Array.from(rows).map(row => parseInt(row.dataset.index));
    const nextIndex = index ?? (usedIndices.length > 0 ? Math.max(...usedIndices) + 1 : 1);
    const row = createWakeupRow(nextIndex, phrase, moods, hasAudio);
    container.appendChild(row);
    return row;
}

function bindWakeupClips() {
    const wakeupSoundList = document.getElementById("wakeup-sound-list");
    if (!wakeupSoundList) return;

    if (wakeupSoundList.dataset.bound !== "true") {
        wakeupSoundList.dataset.bound = "true";
        wakeupSoundList.addEventListener("click", async (e) => {
            const row = e.target.closest("div[data-index]");
            if (!row) return;
            const clipIndex = row.dataset.index;
            const input = row.querySelector(".wakeup-phrase-input");
            const phrase = input && input.value && input.value.trim();

            if (e.target.closest(".wakeup-mood-toggle")) {
                const panel = row.querySelector(".wakeup-mood-panel");
                if (panel) panel.classList.toggle("hidden");
                return;
            }

            if (e.target.closest(".wakeup-mood-pill")) {
                const pill = e.target.closest(".wakeup-mood-pill");
                const isActive = pill.getAttribute("aria-pressed") === "true";
                pill.setAttribute("aria-pressed", isActive ? "false" : "true");
                pill.classList.toggle("secondary-action--active-cyan", !isActive);
                updateWakeupMoodSummary(row);
                return;
            }

            if (e.target.closest(".wakeup-play-btn")) {
                const tryPlay = async () => {
                    const currentPersona = getCurrentPersonaName();
                    const res = await fetch("/wakeup/play", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ index: parseInt(clipIndex), persona: currentPersona }),
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.error || "Failed to play audio");
                    showNotification(data.status, "success");
                };
                try {
                    await tryPlay();
                } catch (err) {
                    console.warn("Initial play failed, trying to stop service and retry:", err.message);
                    try {
                        await fetch("/service/stop");
                        await ServiceStatus.fetchStatus();
                        await tryPlay();
                        showNotification("Billy was active. Stopped and retried clip.", "warning");
                    } catch (retryErr) {
                        console.error("Retry failed:", retryErr);
                        showNotification("Play failed after retry: " + retryErr.message, "error");
                    }
                }
                return;
            }

            if (e.target.closest(".wakeup-generate-btn")) {
                const generateBtn = e.target.closest("button");
                generateBtn.disabled = true;
                generateBtn.classList.add("opacity-50");
                generateBtn.querySelector("i").textContent = "hourglass_empty";
                if (!phrase) {
                    showNotification("Please enter a phrase", "warning");
                    generateBtn.disabled = false;
                    generateBtn.classList.remove("opacity-50");
                    generateBtn.querySelector("i").textContent = "auto_fix_high";
                    return;
                }
                try {
                    const currentPersona = getCurrentPersonaName();
                    const moods = getWakeupRowMoods(row);
                    const res = await fetch("/wakeup/generate", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            text: phrase,
                            index: parseInt(clipIndex),
                            persona: currentPersona,
                            moods: moods,
                        }),
                    });
                    if (!res.ok) {
                        const err = await res.json();
                        throw new Error(err.error || "Failed to generate audio");
                    }
                    await saveWakeupPhrase(clipIndex, phrase, moods);
                    showNotification(`Clip ${clipIndex} generated and saved!`, "success");
                    await loadWakeupClips();
                } catch (err) {
                    console.error("Generate error:", err);
                    showNotification("Generate failed: " + err.message, "error");
                } finally {
                    generateBtn.disabled = false;
                    generateBtn.classList.remove("opacity-50");
                    generateBtn.querySelector("i").textContent = "auto_fix_high";
                }
                return;
            }

            if (e.target.closest(".remove-wakeup-row")) {
                if (!confirm("Are you sure you want to delete this wake-up clip?")) return;
                try {
                    const res = await fetch("/wakeup/remove", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ index: parseInt(clipIndex), persona: getCurrentPersonaName() }),
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.error || "Failed to remove clip");
                    showNotification(`Clip ${clipIndex} removed`, "success");
                    await loadWakeupClips();
                } catch (err) {
                    console.error("Remove error:", err);
                    showNotification("Remove failed: " + err.message, "error");
                }
            }
        });

    }

    const ideasBtn = document.getElementById("generate-wakeup-ideas-btn");
    if (ideasBtn && ideasBtn.dataset.bound !== "true") {
        ideasBtn.dataset.bound = "true";
        ideasBtn.addEventListener("click", async () => {
            ideasBtn.disabled = true;
            ideasBtn.classList.add("opacity-50");
            const icon = ideasBtn.querySelector(".material-icons");
            const previousIcon = icon ? icon.textContent : "";
            if (icon) icon.textContent = "hourglass_empty";
            try {
                const res = await fetch("/wakeup/ideas", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({persona: getCurrentPersonaName(), count: 5}),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || "Failed to generate ideas");
                const ideas = data.ideas || [];
                const savedIdeas = [];
                for (const idea of ideas) {
                    const phrase = (idea.phrase || "").trim();
                    if (!phrase) continue;
                    const row = addWakeupSound(null, phrase, false, idea.moods || []);
                    const index = row && row.dataset.index;
                    if (!index) continue;
                    await saveWakeupPhrase(index, phrase, idea.moods || []);
                    savedIdeas.push(idea);
                }
                await loadWakeupClips();
                showNotification(`Saved ${savedIdeas.length} wake-up sound ideas`, "success");
            } catch (err) {
                console.error("Wake-up ideas error:", err);
                showNotification("Failed to generate wake-up ideas: " + err.message, "error");
            } finally {
                ideasBtn.disabled = false;
                ideasBtn.classList.remove("opacity-50");
                if (icon) icon.textContent = previousIcon || "auto_awesome";
            }
        });
    }
}

window.bindWakeupClips = bindWakeupClips;
window.addWakeupSound = addWakeupSound;
window.collectWakeupRows = collectWakeupRows;
