// React Query hooks. The rail and run list poll so agent progress shows up without a reload.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type JobQuery, type JobSummary, type Stage } from "./api";

export const useMeta = () => useQuery({ queryKey: ["meta"], queryFn: api.meta, refetchInterval: 5000 });
export const useJobs = (q: JobQuery) => useQuery({ queryKey: ["jobs", q], queryFn: () => api.jobs(q), placeholderData: (prev) => prev });
export const useJob = (id: number | null) => useQuery({ queryKey: ["job", id], queryFn: () => api.job(id!), enabled: id != null });
export const useApplications = () => useQuery({ queryKey: ["applications"], queryFn: () => api.applications(), refetchInterval: 5000 });
export const useRuns = () => useQuery({ queryKey: ["runs"], queryFn: () => api.runs(60), refetchInterval: 4000 });
export const useAnswers = () => useQuery({ queryKey: ["answers"], queryFn: api.answers });
export const useSettings = () => useQuery({ queryKey: ["settings"], queryFn: api.settings });
export const useSystem = () => useQuery({ queryKey: ["system"], queryFn: api.system });
export const useTableCounts = () => useQuery({ queryKey: ["tables"], queryFn: api.tableCounts, refetchInterval: 10000 });

/** Status move with an optimistic update so drag-and-drop feels instant. */
export function useMoveJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status }: { id: number; status: Stage }) => api.patchJob(id, { status }),
    onMutate: async ({ id, status }) => {
      await qc.cancelQueries({ queryKey: ["jobs"] });
      const previous = qc.getQueriesData<{ total: number; items: JobSummary[] }>({ queryKey: ["jobs"] });
      previous.forEach(([key, data]) => {
        if (data) qc.setQueryData(key, { ...data, items: data.items.map((j) => (j.id === id ? { ...j, status } : j)) });
      });
      return { previous };
    },
    onError: (_e, _v, context) => context?.previous.forEach(([key, data]) => qc.setQueryData(key, data)),
    onSettled: (_d, _e, { id }) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["meta"] });
      qc.invalidateQueries({ queryKey: ["job", id] });
    },
  });
}
