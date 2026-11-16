/**
 * Multi-host sync on the dashboard: which node a row came from, and the node filter.
 *
 * A standalone node records no origin and shows none of this. On a node that syncs,
 * session rows (teams, agents, events, activities, workflow runs) carry the node
 * whose session wrote them; an empty origin marks rows from before the cut-over.
 */

export type NodeRole = 'standalone' | 'edge' | 'center';

export interface SyncNodes {
  role: NodeRole;
  node_id: string;
  nodes: string[];
  center_url: string;
}

/** The filter value that lets every node through. */
export const EVERY_NODE = null;

/** Whether this node shows origins at all (it syncs). */
export function showsOrigins(nodes: SyncNodes | undefined): boolean {
  return !!nodes && nodes.role !== 'standalone';
}

/** The filter choices: every node this one holds rows of, then the rows from before the cut-over. */
export function nodeChoices(nodes: SyncNodes | undefined): string[] {
  if (!showsOrigins(nodes)) return [];
  return [...new Set(nodes!.nodes.filter(Boolean)), ''];
}

/** Whether a row of ``origin`` passes the filter (null: every node). */
export function matchesNode(origin: string | undefined, filter: string | null): boolean {
  return filter === EVERY_NODE || (origin ?? '') === filter;
}

/** Rows that pass the filter, in their order. */
export function byNode<T extends { origin_node?: string }>(rows: T[], filter: string | null): T[] {
  return filter === EVERY_NODE ? rows : rows.filter((row) => matchesNode(row.origin_node, filter));
}

/** The ``origin`` query parameter of a list request, when a node is chosen. */
export function originParam(filter: string | null): Record<string, string> {
  return filter === EVERY_NODE ? {} : { origin: filter };
}
