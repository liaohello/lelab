import React, { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { NumberInput } from '@/components/ui/number-input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ConfigComponentProps } from '../types';
import { DatasetItem } from '@/lib/replayApi';
import WandbInstallDialog from '../WandbInstallDialog';
import { useApi } from '@/contexts/ApiContext';
import DatasetMultiSelect from '../DatasetMultiSelect';
import CompositePanel from '../CompositePanel';
import { CompositeReport } from '@/lib/compositesApi';

interface EssentialsCardProps extends ConfigComponentProps {
  datasets: DatasetItem[];
  datasetsLoading: boolean;
  onCompositeReport?: (report: CompositeReport | null) => void;
}

const EssentialsCard: React.FC<EssentialsCardProps> = ({
  config,
  updateConfig,
  datasets,
  datasetsLoading,
  onCompositeReport,
}) => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [wandbDialogOpen, setWandbDialogOpen] = useState(false);
  const [wandbInstallHint, setWandbInstallHint] = useState('pip install wandb');

  const handleWandbToggle = async (checked: boolean) => {
    if (!checked) {
      updateConfig('wandb_enable', false);
      return;
    }
    // Check availability before flipping the switch on. If wandb isn't
    // importable in this lelab process, surface the same install flow used
    // for the training extra (accelerate) instead of letting the user start
    // a run that will fail.
    try {
      const r = await fetchWithHeaders(`${baseUrl}/system/wandb-extra`);
      const data: { available: boolean; install_hint: string } = await r.json();
      if (data.available) {
        updateConfig('wandb_enable', true);
      } else {
        setWandbInstallHint(data.install_hint);
        setWandbDialogOpen(true);
      }
    } catch {
      // Backend unreachable — let the user proceed; training start will
      // surface the real error if wandb is genuinely missing.
      updateConfig('wandb_enable', true);
    }
  };

  return (
    <Card className="bg-slate-800/50 border-slate-700 rounded-xl">
      <CardHeader>
        <CardTitle className="text-white">Run Configuration</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-3">
          <div>
            <Label className="text-slate-300">Dataset Repository ID *</Label>
            <div className="mt-1">
              <DatasetMultiSelect
                datasets={datasets}
                loading={datasetsLoading}
                value={config.dataset_repo_ids ?? (config.dataset_repo_id ? [config.dataset_repo_id] : [])}
                onChange={(repoIds) => {
                  updateConfig('dataset_repo_ids', repoIds);
                  updateConfig('dataset_repo_id', repoIds[0] ?? '');
                  if (repoIds.length < 2) {
                    updateConfig('dataset_weights_mode', 'frames');
                    updateConfig('dataset_weights', undefined);
                  }
                }}
              />
            </div>
            <p className="text-xs text-slate-500 mt-1">
              单个数据集直接训练；选择多个（≥2）时会在训练机上生成只读组合数据集。
              顺序即合并顺序，摄像头命名/维度/fps 必须完全一致。
            </p>
          </div>

          <CompositePanel
            repoIds={config.dataset_repo_ids ?? []}
            weightsMode={config.dataset_weights_mode ?? 'frames'}
            weights={config.dataset_weights ?? []}
            onWeightsModeChange={(mode) => updateConfig('dataset_weights_mode', mode)}
            onWeightsChange={(weights) => updateConfig('dataset_weights', weights)}
            onReport={(report) => onCompositeReport?.(report)}
          />
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <Label htmlFor="policy_type" className="text-slate-300">
              Policy
            </Label>
            <Select
              value={config.policy_type}
              onValueChange={(value) => updateConfig('policy_type', value)}
            >
              <SelectTrigger id="policy_type" className="bg-slate-900 border-slate-600 text-white rounded-lg">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-slate-800 border-slate-600 text-white">
                <SelectItem value="act">ACT (Action Chunking Transformer)</SelectItem>
                <SelectItem value="diffusion">Diffusion Policy</SelectItem>
                <SelectItem value="pi0">PI0</SelectItem>
                <SelectItem value="pi05">PI05</SelectItem>
                <SelectItem value="smolvla">SmolVLA</SelectItem>
                <SelectItem value="fastwam">FastWAM</SelectItem>
                <SelectItem value="fineart_vla">FineART-VLA</SelectItem>
                <SelectItem value="groot">GR00T N1.7</SelectItem>
                <SelectItem value="tdmpc">TD-MPC</SelectItem>
                <SelectItem value="vqbet">VQ-BeT</SelectItem>
                <SelectItem value="pi0_fast">PI0 Fast</SelectItem>
                <SelectItem value="sac">SAC</SelectItem>
                <SelectItem value="reward_classifier">Reward Classifier</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <Label htmlFor="steps" className="text-slate-300">
              Training Steps
            </Label>
            <NumberInput
              id="steps"
              value={config.steps}
              onChange={(v) => {
                if (v !== undefined) updateConfig('steps', v);
              }}
              className="bg-slate-900 border-slate-600 text-white rounded-lg"
            />
          </div>

          {/* FastWAM only. Its config defaults to 7 action / 8 state dims, which
              describes a different robot body; the backend refuses to build the
              command without both, so they must be visible here. */}
          {config.policy_type === "fastwam" && (
            <>
              <div>
                <Label htmlFor="policy_action_dim" className="text-slate-300">
                  Action Dim <span className="text-rose-400">*</span>
                </Label>
                <NumberInput
                  id="policy_action_dim"
                  value={config.policy_action_dim}
                  onChange={(v) => updateConfig('policy_action_dim', v)}
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
                <p className="mt-1 text-xs text-slate-400">
                  6 for a single SO-101 arm, 12 for the bimanual pair.
                </p>
              </div>

              <div>
                <Label htmlFor="policy_proprio_dim" className="text-slate-300">
                  Proprio Dim <span className="text-rose-400">*</span>
                </Label>
                <NumberInput
                  id="policy_proprio_dim"
                  value={config.policy_proprio_dim}
                  onChange={(v) => updateConfig('policy_proprio_dim', v)}
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
                <p className="mt-1 text-xs text-slate-400">
                  Must match the observation.state width of the dataset.
                </p>
              </div>
            </>
          )}

          <div>
            <Label htmlFor="batch_size" className="text-slate-300">
              Batch Size
            </Label>
            <NumberInput
              id="batch_size"
              value={config.batch_size}
              onChange={(v) => {
                if (v !== undefined) updateConfig('batch_size', v);
              }}
              className="bg-slate-900 border-slate-600 text-white rounded-lg"
            />
          </div>

          <div className="flex items-center space-x-3 pt-6">
            <Switch
              id="wandb_enable"
              checked={config.wandb_enable}
              onCheckedChange={handleWandbToggle}
              className="data-[state=checked]:bg-green-500"
            />
            <Label htmlFor="wandb_enable" className="text-slate-300">
              Enable Weights & Biases
            </Label>
          </div>
        </div>

        <div>
          <Label htmlFor="policy_pretrained_path" className="text-slate-300">
            Pretrained policy (optional)
          </Label>
          <Input
            id="policy_pretrained_path"
            value={config.policy_pretrained_path || ''}
            onChange={(e) =>
              updateConfig('policy_pretrained_path', e.target.value || undefined)
            }
            placeholder="lerobot/model-name or /remote/path/to/pretrained_model"
            className="mt-1 bg-slate-900 border-slate-600 text-white rounded-lg"
          />
          <p className="text-xs text-slate-500 mt-1">
            Leave empty to train from scratch. Set a model ID or a path visible to the selected compute target to fine-tune its weights.
          </p>
        </div>

        {/* Adapter training. A 6B backbone needs ~72 GB of training state for a
            full fine-tune, which does not fit a single 32-48 GB card; LoRA keeps
            the backbone frozen and trains rank-decomposed q/v projections plus
            the layers the base checkpoint does not contain. */}
        <div className="space-y-3 rounded-lg border border-slate-700 p-3">
          <div className="flex items-center space-x-3">
            <Switch
              id="use_peft"
              checked={config.use_peft ?? false}
              onCheckedChange={(checked) => updateConfig('use_peft', checked)}
              className="data-[state=checked]:bg-green-500"
            />
            <Label htmlFor="use_peft" className="text-slate-300">
              LoRA fine-tuning
            </Label>
          </div>
          <p className="text-xs text-slate-500">
            Requires a pretrained policy above. Full fine-tuning needs far more VRAM than adapter training.
          </p>
          {config.use_peft && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <Label htmlFor="peft_r" className="text-slate-300">
                  LoRA rank
                </Label>
                <NumberInput
                  id="peft_r"
                  value={config.peft_r}
                  onChange={(v) => updateConfig('peft_r', v)}
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
              <div>
                <Label htmlFor="peft_alpha" className="text-slate-300">
                  LoRA alpha
                </Label>
                <NumberInput
                  id="peft_alpha"
                  value={config.peft_alpha}
                  onChange={(v) => updateConfig('peft_alpha', v)}
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
            </div>
          )}
          {config.use_peft && !config.policy_pretrained_path && (
            <p className="text-xs text-rose-300">
              Set a pretrained policy — the backend refuses adapter training from scratch.
            </p>
          )}
        </div>

        <WandbInstallDialog
          open={wandbDialogOpen}
          onOpenChange={setWandbDialogOpen}
          installHint={wandbInstallHint}
        />

        {config.wandb_enable && (
          <div>
            <Label htmlFor="wandb_project" className="text-slate-300">
              W&B Project Name
            </Label>
            <Input
              id="wandb_project"
              value={config.wandb_project || ''}
              onChange={(e) =>
                updateConfig('wandb_project', e.target.value || undefined)
              }
              placeholder="my-robotics-project"
              className="bg-slate-900 border-slate-600 text-white rounded-lg"
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
};

export default EssentialsCard;
