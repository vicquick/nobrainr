/* Timeline view rendering */

let tlOffset = 0;
const TL_LIMIT = 50;

async function loadTimeline(reset) {
    if (reset !== false) tlOffset = 0;

    const category = document.getElementById('tl-category').value;
    const machine = document.getElementById('tl-machine').value;

    let url = `/api/timeline?limit=${TL_LIMIT}&offset=${tlOffset}`;
    if (category) url += `&category=${encodeURIComponent(category)}`;
    if (machine) url += `&source_machine=${encodeURIComponent(machine)}`;

    const resp = await fetch(url);
    const memories = await resp.json();

    const container = document.getElementById('timeline');

    if (tlOffset === 0) {
        container.innerHTML = '';
    }

    if (memories.length === 0 && tlOffset === 0) {
        container.innerHTML = '<p class="empty">No memories found.</p>';
        return;
    }

    let currentDate = '';
    memories.forEach(mem => {
        const date = mem.created_at ? mem.created_at.substring(0, 10) : 'unknown';

        if (date !== currentDate) {
            currentDate = date;
            const header = document.createElement('div');
            header.className = 'timeline-date-header';
            header.style.cssText = 'font-size:0.85rem;color:#58a6ff;margin:1rem 0 0.5rem;font-weight:600;';
            header.textContent = date;
            container.appendChild(header);
        }

        const item = document.createElement('div');
        item.className = 'timeline-item';
        item.innerHTML = `
            ${mem.summary ? `<h4>${escHtml(mem.summary)}</h4>` : ''}
            <p>${escHtml((mem.content || '').substring(0, 200))}${(mem.content || '').length > 200 ? '...' : ''}</p>
            <div class="timeline-badges">
                <span class="badge category">${escHtml(mem.category || 'uncategorized')}</span>
                ${mem.source_machine ? `<span class="badge machine">${escHtml(mem.source_machine)}</span>` : ''}
                ${mem.importance ? `<span class="badge importance">imp: ${Number(mem.importance).toFixed(2)}</span>` : ''}
            </div>
        `;
        container.appendChild(item);
    });

    tlOffset += memories.length;

    const btn = document.getElementById('tl-load-more');
    btn.style.display = memories.length < TL_LIMIT ? 'none' : '';
}

function loadMore() {
    loadTimeline(false);
}

function escHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// Init
document.addEventListener('DOMContentLoaded', function() {
    // Populate filter dropdowns
    fetch('/api/categories')
        .then(r => r.json())
        .then(cats => {
            const sel = document.getElementById('tl-category');
            cats.forEach(c => {
                const opt = document.createElement('option');
                opt.value = c; opt.textContent = c;
                sel.appendChild(opt);
            });
        });

    fetch('/api/stats')
        .then(r => r.json())
        .then(stats => {
            const sel = document.getElementById('tl-machine');
            (stats.by_machine || []).forEach(m => {
                const opt = document.createElement('option');
                opt.value = m.source_machine; opt.textContent = m.source_machine;
                sel.appendChild(opt);
            });
        });

    loadTimeline();
});
