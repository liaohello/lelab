import React from "react";
import { ArrowDown, ArrowUp, Check, ChevronsUpDown, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { DatasetItem } from "@/lib/replayApi";

interface Props {
  datasets: DatasetItem[];
  loading: boolean;
  /** Selected repo ids, in composite order (order matters). */
  value: string[];
  onChange: (repoIds: string[]) => void;
  /** Dataset ids that cannot be combined (e.g. Hub-only entries). */
  disabledRepoIds?: string[];
}

const MAX_MEMBERS = 8;

const DatasetMultiSelect: React.FC<Props> = ({
  datasets,
  loading,
  value,
  onChange,
  disabledRepoIds = [],
}) => {
  const [open, setOpen] = React.useState(false);

  const toggle = (repoId: string) => {
    if (value.includes(repoId)) {
      onChange(value.filter((item) => item !== repoId));
      return;
    }
    if (value.length >= MAX_MEMBERS) return;
    onChange([...value, repoId]);
  };

  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= value.length) return;
    const next = [...value];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };

  const local = datasets.filter((d) => d.source === "local" || d.source === "both");
  const hub = datasets.filter((d) => d.source === "hub");

  const renderItem = (d: DatasetItem) => {
    const selected = value.includes(d.repo_id);
    const disabled = disabledRepoIds.includes(d.repo_id);
    return (
      <CommandItem
        key={d.repo_id}
        value={d.repo_id}
        onSelect={() => !disabled && toggle(d.repo_id)}
        className={cn(
          "text-white aria-selected:bg-gray-700",
          disabled && "cursor-not-allowed opacity-50",
        )}
      >
        <Check className={cn("mr-2 h-4 w-4", selected ? "opacity-100" : "opacity-0")} />
        <span className="flex-1 truncate">{d.repo_id}</span>
        {d.source === "both" && <span className="mr-2 text-xs text-gray-400">on Hub</span>}
        {disabled && <span className="text-xs text-amber-400">需先下载</span>}
      </CommandItem>
    );
  };

  return (
    <div className="space-y-2">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            variant="outline"
            role="combobox"
            aria-expanded={open}
            className="w-full justify-between bg-gray-800 border-gray-600 text-white hover:bg-gray-700"
          >
            {loading
              ? "Loading datasets…"
              : value.length === 0
              ? "Select one or more datasets…"
              : `已选择 ${value.length} 个数据集`}
            <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-[--radix-popover-trigger-width] border-gray-600 bg-gray-800 p-0">
          <Command className="bg-gray-800">
            <CommandInput placeholder="Search datasets…" className="text-white" />
            <CommandList>
              <CommandEmpty>No dataset found.</CommandEmpty>
              <CommandGroup heading="Local">{local.map(renderItem)}</CommandGroup>
              <CommandGroup heading="HuggingFace Hub">
                {hub.map((d) => renderItem({ ...d }))}
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>

      {value.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {value.map((repoId, index) => (
            <Badge
              key={repoId}
              className="flex items-center gap-1 border-slate-600 bg-slate-900 py-1 pl-2 pr-1 text-slate-200"
            >
              <span className="text-xs text-slate-500">{index + 1}</span>
              <span className="max-w-[180px] truncate" title={repoId}>
                {repoId}
              </span>
              <button
                type="button"
                className="rounded p-0.5 hover:bg-slate-700 disabled:opacity-30"
                onClick={() => move(index, -1)}
                disabled={index === 0}
                aria-label={`上移 ${repoId}`}
              >
                <ArrowUp className="h-3 w-3" />
              </button>
              <button
                type="button"
                className="rounded p-0.5 hover:bg-slate-700 disabled:opacity-30"
                onClick={() => move(index, 1)}
                disabled={index === value.length - 1}
                aria-label={`下移 ${repoId}`}
              >
                <ArrowDown className="h-3 w-3" />
              </button>
              <button
                type="button"
                className="rounded p-0.5 hover:bg-slate-700"
                onClick={() => onChange(value.filter((item) => item !== repoId))}
                aria-label={`移除 ${repoId}`}
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
};

export default DatasetMultiSelect;
