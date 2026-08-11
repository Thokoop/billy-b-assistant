/**
 * Mood panel rendering for the user profile page.
 */
const MoodPanel = (() => {
    const getContentElement = () => (
        document.getElementById('mood-content') ||
        document.getElementById('mood-content-main')
    );

    const moodMeta = {
        neutral: {icon: 'sentiment_neutral', color: 'text-zinc-300'},
        calm: {icon: 'spa', color: 'text-teal-300'},
        cheerful: {icon: 'sentiment_satisfied', color: 'text-emerald-300'},
        warm: {icon: 'favorite', color: 'text-rose-200'},
        curious: {icon: 'psychology', color: 'text-cyan-300'},
        focused: {icon: 'center_focus_strong', color: 'text-blue-300'},
        playful: {icon: 'toys', color: 'text-lime-300'},
        mischievous: {icon: 'sentiment_very_satisfied', color: 'text-lime-300'},
        excited: {icon: 'celebration', color: 'text-amber-300'},
        surprised: {icon: 'priority_high', color: 'text-yellow-300'},
        sleepy: {icon: 'bedtime', color: 'text-indigo-300'},
        bored: {icon: 'sentiment_neutral', color: 'text-stone-300'},
        sad: {icon: 'sentiment_very_dissatisfied', color: 'text-sky-300'},
        anxious: {icon: 'sync_problem', color: 'text-orange-300'},
        flustered: {icon: 'sync_problem', color: 'text-orange-300'},
        annoyed: {icon: 'sentiment_dissatisfied', color: 'text-rose-300'},
        grumpy: {icon: 'mood_bad', color: 'text-red-300'},
        dramatic: {icon: 'theater_comedy', color: 'text-fuchsia-300'},
    };

    const bucketLabels = ['min', 'low', 'med', 'high', 'max'];
    const clamp = (value) => Math.max(0, Math.min(100, Number(value) || 0));

    const bucketForValue = (rawValue) => {
        const value = clamp(rawValue);
        if (value < 20) return {index: 0, label: 'min', color: 'bg-rose-500'};
        if (value < 40) return {index: 1, label: 'low', color: 'bg-orange-500'};
        if (value < 65) return {index: 2, label: 'med', color: 'bg-amber-500'};
        if (value < 85) return {index: 3, label: 'high', color: 'bg-emerald-500'};
        return {index: 4, label: 'max', color: 'bg-violet-500'};
    };

    const renderDimension = ([label, rawValue]) => {
        const value = clamp(rawValue);
        const activeBucket = bucketForValue(value);

        return `
            <div class="grid grid-cols-[6.75rem_1fr] items-center gap-3" title="${label}: ${value}">
                <div class="text-sm text-zinc-300 truncate">${label}</div>
                <div class="grid grid-cols-5 gap-1.5">
                    ${bucketLabels.map((bucketLabel, index) => {
                        const isActive = index === activeBucket.index;
                        const activeClass = isActive ? `${activeBucket.color} text-white` : 'bg-zinc-700/80 text-transparent';
                        return `
                            <div class="h-8 rounded flex items-center justify-center text-xs font-medium ${activeClass}" aria-label="${label} ${bucketLabel}">
                                ${isActive ? activeBucket.label : ''}
                            </div>
                        `;
                    }).join('')}
                </div>
            </div>
        `;
    };

    const render = (mood) => {
        const moodContent = getContentElement();
        if (!moodContent) return;

        if (!mood || !mood.label) {
            moodContent.innerHTML = '<p class="text-sm text-zinc-400 italic">No mood data yet</p>';
            return;
        }

        const meta = moodMeta[mood.label] || moodMeta.neutral;
        const dimensions = [
            ['Positivity', mood.positivity],
            ['Energy', mood.energy],
            ['Irritability', mood.irritability],
            ['Engagement', mood.engagement],
            ['Composure', mood.composure],
        ];
        const lastEvent = mood.last_event ? mood.last_event.split('_').join(' ') : 'none';

        moodContent.innerHTML = `
            <div class="p-3 bg-zinc-800/50 rounded-lg border border-zinc-600">
                <div class="flex items-center justify-between gap-3 mb-4">
                    <div class="flex items-center gap-3">
                        <span class="material-icons ${meta.color}">${meta.icon}</span>
                        <div>
                            <div class="text-white capitalize">${mood.label}</div>
                            <div class="text-xs text-zinc-400">Last event: ${lastEvent}</div>
                        </div>
                    </div>
                </div>
                <div class="space-y-3">
                    ${dimensions.map(renderDimension).join('')}
                </div>
            </div>
        `;
    };

    const load = async (status = null) => {
        const moodContent = getContentElement();
        if (!moodContent) return;

        try {
            const statusData = status || await ServiceStatus.fetchStatus();
            render(statusData?.mood || null);
        } catch (error) {
            console.error('Failed to load mood:', error);
            moodContent.innerHTML = '<p class="text-sm text-zinc-400 italic">Error loading mood</p>';
        }
    };

    const api = {load, render};
    window.MoodPanel = api;
    return api;
})();
