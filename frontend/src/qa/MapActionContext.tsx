import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import type { QaMapAction } from "../types";

export interface MapActionState {
  sequence: number;
  eventId: string | null;
  action: QaMapAction | null;
}

interface MapActionContextValue {
  currentEventId: string | null;
  state: MapActionState;
  publish: (eventId: string, action: QaMapAction) => void;
}

const EMPTY_STATE: MapActionState = {
  sequence: 0,
  eventId: null,
  action: null,
};

const MapActionContext = createContext<MapActionContextValue | null>(null);

export function MapActionProvider({
  eventId,
  children,
}: {
  eventId?: string | null;
  children: ReactNode;
}) {
  const normalizedEventId = eventId ?? null;
  const [state, setState] = useState<MapActionState>({
    sequence: 0,
    eventId: normalizedEventId,
    action: null,
  });

  useEffect(() => {
    setState((current) => {
      if (current.eventId === normalizedEventId) {
        return current;
      }
      return {
        sequence: current.sequence + 1,
        eventId: normalizedEventId,
        action: null,
      };
    });
  }, [normalizedEventId]);

  const publish = useCallback(
    (actionEventId: string, action: QaMapAction) => {
      setState((current) => {
        if (current.eventId !== actionEventId) {
          return current;
        }
        return {
          sequence: current.sequence + 1,
          eventId: actionEventId,
          action,
        };
      });
    },
    [],
  );

  const value = useMemo(
    () => ({
      currentEventId: normalizedEventId,
      state,
      publish,
    }),
    [normalizedEventId, publish, state],
  );

  return (
    <MapActionContext.Provider value={value}>
      {children}
    </MapActionContext.Provider>
  );
}

export function useMapActionPublisher() {
  const context = useContext(MapActionContext);
  if (!context) {
    throw new Error("useMapActionPublisher must be used within MapActionProvider");
  }
  return context.publish;
}

export function useMapActionConsumer(): MapActionState {
  const context = useContext(MapActionContext);
  return context?.state ?? EMPTY_STATE;
}

export function useMapActionEventId(): string | null {
  const context = useContext(MapActionContext);
  return context?.currentEventId ?? null;
}

export function isQaMapActionExpired(
  action: QaMapAction,
  now = Date.now(),
): boolean {
  if (!action.valid_until) {
    return false;
  }
  const validUntil = Date.parse(action.valid_until);
  return Number.isFinite(validUntil) && validUntil <= now;
}
