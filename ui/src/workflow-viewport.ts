import type { Viewport, XYPosition } from '@xyflow/react';
import type { WorkflowProjection } from './workflow-projection';

/** The canvas node standing for a group: its collapsed card, its region, or a choice's decision. */
export function workflowGroupNodeId(
  projection: Pick<WorkflowProjection, 'nodes' | 'regions'>,
  owner: string
): string | undefined {
  return (
    projection.nodes.find((node) => node.role === 'collapsed' && node.owner === owner)?.id ??
    projection.regions.find((region) => region.owner === owner)?.id ??
    projection.nodes.find((node) => node.role === 'decision' && node.owner === owner)?.id
  );
}

/** The node drawn just before a group's card; an expanded choice may hang its outcomes on it. */
export function workflowGroupEntryId(
  projection: Pick<WorkflowProjection, 'nodes' | 'regions'> & {
    edges: readonly { source: string; target: string }[];
  },
  owner: string
): string | undefined {
  const id = workflowGroupNodeId(projection, owner);
  return id ? projection.edges.find((edge) => edge.target === id)?.source : undefined;
}

/** Same zoom, shifted so a point that moved from `before` to `after` keeps its screen spot. */
export function anchoredViewport(
  viewport: Viewport,
  before: XYPosition,
  after: XYPosition
): Viewport {
  return {
    x: viewport.x - (after.x - before.x) * viewport.zoom,
    y: viewport.y - (after.y - before.y) * viewport.zoom,
    zoom: viewport.zoom,
  };
}
