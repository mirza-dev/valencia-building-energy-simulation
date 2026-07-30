import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'

export const DEFAULT_ACTIVE_BUILDING = '4252702YJ2745A'
export const ACTIVE_BUILDING_STORAGE_KEY = 'workbench-active-refparcela'

interface ActiveBuildingContextValue {
  activeBuilding: string
  setActiveBuilding: (refparcela: string) => void
}

const ActiveBuildingContext = createContext<ActiveBuildingContextValue | null>(null)

export function normalizeActiveBuildingRef(value: unknown): string {
  if (typeof value !== 'string') return DEFAULT_ACTIVE_BUILDING
  const normalized = value.trim().toUpperCase()
  return normalized || DEFAULT_ACTIVE_BUILDING
}

function storedActiveBuilding(): string {
  try {
    return normalizeActiveBuildingRef(localStorage.getItem(ACTIVE_BUILDING_STORAGE_KEY))
  } catch {
    return DEFAULT_ACTIVE_BUILDING
  }
}

export function ActiveBuildingProvider({ children }: { children: ReactNode }) {
  const [activeBuilding, setActiveBuildingState] = useState(storedActiveBuilding)
  const value = useMemo<ActiveBuildingContextValue>(() => ({
    activeBuilding,
    setActiveBuilding: (refparcela: string) => {
      const normalized = normalizeActiveBuildingRef(refparcela)
      localStorage.setItem(ACTIVE_BUILDING_STORAGE_KEY, normalized)
      setActiveBuildingState(normalized)
    },
  }), [activeBuilding])

  return <ActiveBuildingContext.Provider value={value}>{children}</ActiveBuildingContext.Provider>
}

export function useActiveBuilding(): ActiveBuildingContextValue {
  const value = useContext(ActiveBuildingContext)
  if (!value) throw new Error('useActiveBuilding must be used inside ActiveBuildingProvider')
  return value
}
