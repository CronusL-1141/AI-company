import { create } from 'zustand';
import type { WSEvent } from '../types';

interface WSStore {
  connected: boolean;
  events: WSEvent[];
  setConnected: (v: boolean) => void;
  addEvent: (e: WSEvent) => void;
  clearEvents: () => void;
}

export const useWSStore = create<WSStore>((set) => ({
  connected: false,
  events: [],
  setConnected: (connected) => set({ connected }),
  addEvent: (event) =>
    set((state) => ({
      events: [event, ...state.events].slice(0, 200),
    })),
  clearEvents: () => set({ events: [] }),
}));
