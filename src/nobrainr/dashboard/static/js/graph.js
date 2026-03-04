/* Cytoscape.js knowledge graph initialization */

const TYPE_COLORS = {
    person:       '#58a6ff',
    project:      '#3fb950',
    technology:   '#bc8cff',
    concept:      '#d29922',
    file:         '#8b949e',
    config:       '#d29922',
    error:        '#f85149',
    location:     '#3fb950',
    organization: '#58a6ff',
};

let cy = null;
let allElements = { nodes: [], edges: [] };

async function initGraph() {
    const resp = await fetch('/api/graph');
    const data = await resp.json();
    allElements = data;

    cy = cytoscape({
        container: document.getElementById('cy'),
        elements: [...data.nodes, ...data.edges],
        style: [
            {
                selector: 'node',
                style: {
                    'label': 'data(label)',
                    'background-color': function(ele) {
                        return TYPE_COLORS[ele.data('type')] || '#8b949e';
                    },
                    'width': function(ele) {
                        return Math.max(20, Math.min(60, 15 + (ele.data('mention_count') || 1) * 3));
                    },
                    'height': function(ele) {
                        return Math.max(20, Math.min(60, 15 + (ele.data('mention_count') || 1) * 3));
                    },
                    'font-size': '10px',
                    'color': '#e6edf3',
                    'text-outline-color': '#0d1117',
                    'text-outline-width': 2,
                    'text-valign': 'bottom',
                    'text-margin-y': 5,
                    'border-width': 0,
                    'overlay-opacity': 0,
                },
            },
            {
                selector: 'node:selected',
                style: {
                    'border-width': 3,
                    'border-color': '#ffffff',
                },
            },
            {
                selector: 'node.highlighted',
                style: {
                    'border-width': 3,
                    'border-color': '#58a6ff',
                },
            },
            {
                selector: 'node.dimmed',
                style: {
                    'opacity': 0.2,
                },
            },
            {
                selector: 'edge',
                style: {
                    'label': 'data(label)',
                    'width': function(ele) {
                        return Math.max(1, (ele.data('confidence') || 0.5) * 3);
                    },
                    'line-color': '#30363d',
                    'target-arrow-color': '#30363d',
                    'target-arrow-shape': 'triangle',
                    'curve-style': 'bezier',
                    'font-size': '8px',
                    'color': '#8b949e',
                    'text-outline-color': '#0d1117',
                    'text-outline-width': 1.5,
                    'text-rotation': 'autorotate',
                    'overlay-opacity': 0,
                },
            },
            {
                selector: 'edge.dimmed',
                style: {
                    'opacity': 0.1,
                },
            },
        ],
        layout: {
            name: 'fcose',
            quality: 'proof',
            randomize: true,
            animate: true,
            animationDuration: 500,
            nodeRepulsion: function() { return 8000; },
            idealEdgeLength: function() { return 120; },
            edgeElasticity: function() { return 0.45; },
            gravity: 0.25,
            gravityRange: 3.8,
            nodeSeparation: 75,
            numIter: 2500,
            tile: true,
        },
        minZoom: 0.1,
        maxZoom: 5,
        wheelSensitivity: 0.3,
    });

    // Node click → load detail in side panel
    cy.on('tap', 'node', function(evt) {
        const node = evt.target;
        const entityId = node.data('id');

        // Highlight connected
        cy.elements().removeClass('highlighted dimmed');
        const neighborhood = node.neighborhood().add(node);
        cy.elements().not(neighborhood).addClass('dimmed');
        neighborhood.addClass('highlighted');

        // Load detail via HTMX
        const panel = document.getElementById('side-panel');
        const content = document.getElementById('side-panel-content');
        panel.classList.remove('hidden');

        fetch('/api/node/' + entityId, { headers: { 'HX-Request': 'true' } })
            .then(r => r.text())
            .then(html => { content.innerHTML = html; });
    });

    // Click background → deselect
    cy.on('tap', function(evt) {
        if (evt.target === cy) {
            cy.elements().removeClass('highlighted dimmed');
            closeSidePanel();
        }
    });

    // Drag physics — spring-like behavior
    cy.on('grab', 'node', function(evt) {
        evt.target.scratch('_dragging', true);
    });
    cy.on('free', 'node', function(evt) {
        evt.target.scratch('_dragging', false);
    });
}

function filterGraph() {
    const type = document.getElementById('type-filter').value;
    if (!cy) return;

    if (!type) {
        cy.elements().show();
        return;
    }

    cy.nodes().forEach(n => {
        if (n.data('type') === type) {
            n.show();
        } else {
            n.hide();
        }
    });
    cy.edges().forEach(e => {
        const src = cy.getElementById(e.data('source'));
        const tgt = cy.getElementById(e.data('target'));
        if (src.visible() && tgt.visible()) {
            e.show();
        } else {
            e.hide();
        }
    });
}

function searchGraph(query) {
    if (!cy || !query) {
        cy && cy.elements().removeClass('highlighted dimmed');
        return;
    }

    const q = query.toLowerCase();
    cy.elements().removeClass('highlighted dimmed');

    const matches = cy.nodes().filter(n =>
        n.data('label').toLowerCase().includes(q)
    );

    if (matches.length > 0) {
        const neighborhood = matches.neighborhood().add(matches);
        cy.elements().not(neighborhood).addClass('dimmed');
        matches.addClass('highlighted');
        cy.animate({ fit: { eles: matches, padding: 50 } }, { duration: 300 });
    }
}

function resetGraph() {
    if (!cy) return;
    cy.elements().removeClass('highlighted dimmed');
    cy.elements().show();
    cy.animate({ fit: { padding: 30 } }, { duration: 300 });
    document.getElementById('type-filter').value = '';
    document.getElementById('search-input').value = '';
    closeSidePanel();
}

function closeSidePanel() {
    document.getElementById('side-panel').classList.add('hidden');
    cy && cy.elements().removeClass('highlighted dimmed');
}

// Check if fcose layout is available, fallback to cose
document.addEventListener('DOMContentLoaded', function() {
    // fcose needs to be registered as an extension; fallback to cose if not available
    if (typeof cytoscape !== 'undefined') {
        // Try loading fcose from CDN if not already present
        if (!cytoscape.extensions || !cytoscape.extensions().layouts || !cytoscape.extensions().layouts.fcose) {
            // Dynamically load fcose
            const script = document.createElement('script');
            script.src = 'https://unpkg.com/cytoscape-fcose@2.2.0/cytoscape-fcose.js';
            script.onload = initGraph;
            script.onerror = function() {
                // Fallback: use built-in cose layout
                console.warn('fcose not available, using cose layout');
                initGraphFallback();
            };
            document.head.appendChild(script);
        } else {
            initGraph();
        }
    }
});

function initGraphFallback() {
    // Same as initGraph but with cose layout instead of fcose
    fetch('/api/graph')
        .then(r => r.json())
        .then(data => {
            allElements = data;
            // Reuse initGraph but override layout
            const origInit = initGraph;
            initGraph = async function() {
                await origInit();
                if (cy) {
                    cy.layout({
                        name: 'cose',
                        animate: true,
                        animationDuration: 500,
                        nodeRepulsion: function() { return 8000; },
                        idealEdgeLength: function() { return 120; },
                    }).run();
                }
            };
            initGraph();
        });
}
