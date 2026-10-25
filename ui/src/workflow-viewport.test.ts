import test from 'node:test';
import assert from 'node:assert/strict';
import type { Document, GraphNode } from './domain';
import { compactWorkflow } from './workflow-display';
import { projectWorkflow } from './workflow-projection';
import { anchoredViewport, workflowGroupEntryId, workflowGroupNodeId } from './workflow-viewport';

const root: GraphNode = {
  kind: 'seq',
  name: 'run',
  children: [
    { kind: 'step', name: 'prepare' },
    { kind: 'loop', name: 'rounds', maxIterations: 2, body: { kind: 'step', name: 'write' } },
  ],
};
const document = { graph: { root } } as Document;
const project = (collapsed: string[]) =>
  compactWorkflow(projectWorkflow(document, new Set(collapsed)));

test('a group is its collapsed card, then its region once expanded', () => {
  const folded = project(['rounds']);
  const card = folded.nodes.find((node) => node.id === workflowGroupNodeId(folded, 'rounds'));
  assert.equal(card?.role, 'collapsed');
  assert.equal(card?.owner, 'rounds');
  // The expanded loop also owns a loop-end junction; the region stands for the group.
  const open = project([]);
  const region = open.regions.find((value) => value.id === workflowGroupNodeId(open, 'rounds'));
  assert.equal(region?.owner, 'rounds');
  assert.equal(workflowGroupNodeId(open, 'prepare'), undefined);
  assert.equal(workflowGroupNodeId(open, 'missing'), undefined);
});

const routeAfter = (preceding: GraphNode) => {
  const root: GraphNode = {
    kind: 'seq',
    name: 'run',
    children: [
      preceding,
      {
        kind: 'choice',
        name: 'route',
        branches: [
          {
            when: {
              kind: 'in',
              value: { name: 'triage', source: 'signal', field: 'route' },
              labels: ['simple'],
            },
            node: { kind: 'step', name: 'answer' },
          },
        ],
        otherwise: { kind: 'step', name: 'escalate' },
      },
    ],
  };
  return { graph: { root } } as Document;
};

test('an expanded choice is its decision node', () => {
  const open = compactWorkflow(projectWorkflow(routeAfter({ kind: 'step', name: 'triage' })));
  const decision = open.nodes.find((node) => node.id === workflowGroupNodeId(open, 'route'));
  assert.equal(decision?.role, 'decision');
  assert.equal(decision?.owner, 'route');
});

test('a choice attached to its verifier keeps that verifier as the anchor', () => {
  const routed = routeAfter({
    kind: 'verifier',
    name: 'triage',
    signals: { route: ['simple', 'complex'] },
  });
  const folded = compactWorkflow(projectWorkflow(routed, new Set(['route'])));
  const entry = workflowGroupEntryId(folded, 'route');
  assert.equal(folded.nodes.find((node) => node.id === entry)?.owner, 'triage');
  // Expanded, the choice has no node of its own; its outcomes start at that same verifier.
  const open = compactWorkflow(projectWorkflow(routed));
  assert.equal(workflowGroupNodeId(open, 'route'), undefined);
  assert.ok(open.edges.some((edge) => edge.owner === 'route' && edge.source === entry));
});

test('the anchor keeps its screen position and the zoom stays', () => {
  const viewport = { x: 40, y: -20, zoom: 1.5 };
  // (100, 200) is drawn at (190, 280); after moving to (130, 160) it must still be drawn there.
  assert.deepEqual(anchoredViewport(viewport, { x: 100, y: 200 }, { x: 130, y: 160 }), {
    x: -5,
    y: 40,
    zoom: 1.5,
  });
  assert.deepEqual(anchoredViewport(viewport, { x: 100, y: 200 }, { x: 100, y: 200 }), viewport);
});
