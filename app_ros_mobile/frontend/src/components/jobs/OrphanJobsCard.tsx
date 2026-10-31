import React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "@/contexts/ApiContext";
import { useToast } from "@/hooks/use-toast";
import { adoptOrphanJob, listOrphanJobs } from "@/lib/jobsApi";

/**
 * Remote training runs started by this Jetson that the local registry lost
 * (e.g. after a reinstall). Adopting one brings it back under management
 * (monitor / stop / delete); foreign jobs are never listed.
 */
const OrphanJobsCard: React.FC = () => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const orphansQuery = useQuery({
    queryKey: ["orphan-jobs", baseUrl],
    queryFn: ({ signal }) => listOrphanJobs(baseUrl, fetchWithHeaders, signal),
    refetchInterval: 15_000,
    retry: 1,
  });

  const adopt = useMutation({
    mutationFn: (jobId: string) => adoptOrphanJob(baseUrl, fetchWithHeaders, jobId),
    onSuccess: (record) => {
      toast({ title: `已接管 ${record.id}`, description: `状态 ${record.state}` });
      void queryClient.invalidateQueries({ queryKey: ["orphan-jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (error) =>
      toast({
        title: "接管失败",
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      }),
  });

  const orphans = orphansQuery.data ?? [];
  if (orphansQuery.isError && orphans.length === 0) return null;

  return (
    <Card className="border-amber-900/60 bg-amber-950/20 text-slate-100">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 p-4 pb-2">
        <div>
          <CardDescription className="text-amber-200/80">
            训练机上的孤儿作业（本系统启动但本地无记录）
          </CardDescription>
          <CardTitle className="text-sm">
            {orphans.length === 0 ? "没有需要接管的作业" : `${orphans.length} 个可接管`}
          </CardTitle>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="border-amber-800 bg-transparent text-amber-100 hover:bg-amber-950/40"
          onClick={() => void orphansQuery.refetch()}
          disabled={orphansQuery.isFetching}
        >
          <RefreshCw className={`mr-2 h-4 w-4 ${orphansQuery.isFetching ? "animate-spin" : ""}`} />
          刷新
        </Button>
      </CardHeader>
      {orphans.length > 0 ? (
        <CardContent className="space-y-2 p-4 pt-0">
          {orphans.map((job) => (
            <div
              key={job.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded border border-amber-900/50 bg-slate-950/40 p-2 text-xs"
            >
              <span className="truncate font-mono" title={job.id}>
                {job.id}
              </span>
              <span className="text-slate-400">
                {job.state}
                {job.gpu != null ? ` · GPU ${job.gpu}` : ""}
                {job.checkpoint_count ? ` · ${job.checkpoint_count} ckpt` : ""}
              </span>
              <Button
                size="sm"
                variant="outline"
                className="border-emerald-800 bg-transparent text-emerald-200 hover:bg-emerald-950/40"
                disabled={adopt.isPending}
                onClick={() => adopt.mutate(job.id)}
              >
                <UserPlus className="mr-1 h-3.5 w-3.5" /> 接管
              </Button>
            </div>
          ))}
        </CardContent>
      ) : null}
    </Card>
  );
};

export default OrphanJobsCard;
