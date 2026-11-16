import { create } from 'zustand';
import { EVERY_NODE } from '@/lib/nodes';

interface NodeFilterStore {
  /** The node whose rows the session views show; null: every node, '': before the cut-over. */
  node: string | null;
  setNode: (node: string | null) => void;
}

export const useNodeFilter = create<NodeFilterStore>((set) => ({
  node: EVERY_NODE,
  setNode: (node) => set({ node }),
}));
