import React, { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
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
import { Separator } from '@/components/ui/separator';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { ConfigComponentProps } from '../types';

const SectionHeading: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
    {children}
  </h4>
);

const EpisodeListInput: React.FC<{
  value?: number[];
  onChange: (value: number[] | undefined) => void;
}> = ({ value, onChange }) => {
  const [text, setText] = useState((value ?? []).join(', '));
  useEffect(() => {
    setText((value ?? []).join(', '));
  }, [value]);
  return (
    <Input
      value={text}
      onChange={(event) => setText(event.target.value)}
      onBlur={() => {
        const parts = text
          .split(',')
          .map((part) => part.trim())
          .filter((part) => part.length > 0);
        const parsed = parts.map(Number);
        onChange(
          parsed.length > 0 && parsed.every((value) => Number.isInteger(value) && value >= 0)
            ? parsed
            : undefined,
        );
      }}
      placeholder="例如 0,1,2,3,4,5,6,7,8,9,10,11"
      className="bg-slate-900 border-slate-600 text-white rounded-lg"
    />
  );
};

const AdvancedCard: React.FC<ConfigComponentProps> = ({ config, updateConfig }) => {
  const [expanded, setExpanded] = useState(false);

  return (
    <Card className="bg-slate-800/50 border-slate-700 rounded-xl">
      <CardHeader
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
        className="cursor-pointer select-none flex flex-row items-center justify-between"
      >
        <span className="text-white font-semibold">Advanced</span>
        <span className="flex items-center gap-1 text-slate-400 text-sm">
          {expanded ? (
            <ChevronDown className="w-4 h-4" />
          ) : (
            <ChevronRight className="w-4 h-4" />
          )}
          {expanded ? 'Hide' : 'Show'}
        </span>
      </CardHeader>

      {expanded && (
        <CardContent className="space-y-8">
          {/* Policy */}
          <section className="space-y-4">
            <SectionHeading>Policy</SectionHeading>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <Label htmlFor="policy_device" className="text-slate-300">
                  Device
                </Label>
                <Select
                  value={config.policy_device || 'cuda'}
                  onValueChange={(value) => updateConfig('policy_device', value)}
                >
                  <SelectTrigger id="policy_device" className="bg-slate-900 border-slate-600 text-white rounded-lg">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent className="bg-slate-800 border-slate-600 text-white">
                    <SelectItem value="cuda">CUDA (GPU)</SelectItem>
                    <SelectItem value="cpu">CPU</SelectItem>
                    <SelectItem value="mps">MPS (Apple Silicon)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="flex items-center space-x-3 pt-6">
                <Switch
                  id="policy_use_amp"
                  checked={config.policy_use_amp}
                  onCheckedChange={(checked) => updateConfig('policy_use_amp', checked)}
                />
                <Label htmlFor="policy_use_amp" className="text-slate-300">
                  Use Automatic Mixed Precision
                </Label>
              </div>
            </div>
          </section>

          <Separator className="bg-slate-700" />

          {/* Training */}
          <section className="space-y-4">
            <SectionHeading>Training</SectionHeading>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <Label htmlFor="seed" className="text-slate-300">
                  Random Seed
                </Label>
                <NumberInput
                  id="seed"
                  value={config.seed}
                  onChange={(v) => updateConfig('seed', v)}
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
              <div>
                <Label htmlFor="num_workers" className="text-slate-300">
                  Number of Workers
                </Label>
                <NumberInput
                  id="num_workers"
                  value={config.num_workers}
                  onChange={(v) => {
                    if (v !== undefined) updateConfig('num_workers', v);
                  }}
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
            </div>
          </section>

          <Separator className="bg-slate-700" />

          {/* Optimizer */}
          <section className="space-y-4">
            <SectionHeading>Optimizer</SectionHeading>
            <div>
              <Label htmlFor="optimizer_type" className="text-slate-300">
                Optimizer
              </Label>
              <Select
                value={config.optimizer_type || 'adam'}
                onValueChange={(value) => updateConfig('optimizer_type', value)}
              >
                <SelectTrigger id="optimizer_type" className="bg-slate-900 border-slate-600 text-white rounded-lg">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-slate-800 border-slate-600 text-white">
                  <SelectItem value="adam">Adam</SelectItem>
                  <SelectItem value="adamw">AdamW</SelectItem>
                  <SelectItem value="sgd">SGD</SelectItem>
                  <SelectItem value="multi_adam">Multi Adam</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <Label htmlFor="optimizer_lr" className="text-slate-300">
                  Learning Rate
                </Label>
                <NumberInput
                  id="optimizer_lr"
                  integer={false}
                  step="0.0001"
                  value={config.optimizer_lr}
                  onChange={(v) => updateConfig('optimizer_lr', v)}
                  placeholder="Policy default (SmolVLA preset 1e-4)"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
                <p className="mt-1 text-xs text-slate-500">
                  该值在 policy preset 之后应用，并可在监控页核对实际 resolved LR。
                </p>
              </div>
              <div>
                <Label htmlFor="optimizer_weight_decay" className="text-slate-300">
                  Weight Decay
                </Label>
                <NumberInput
                  id="optimizer_weight_decay"
                  integer={false}
                  step="0.0001"
                  value={config.optimizer_weight_decay}
                  onChange={(v) => updateConfig('optimizer_weight_decay', v)}
                  placeholder="Use policy default"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
              <div>
                <Label htmlFor="optimizer_grad_clip_norm" className="text-slate-300">
                  Gradient Clipping
                </Label>
                <NumberInput
                  id="optimizer_grad_clip_norm"
                  integer={false}
                  step="0.0001"
                  value={config.optimizer_grad_clip_norm}
                  onChange={(v) => updateConfig('optimizer_grad_clip_norm', v)}
                  placeholder="Use policy default"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
            </div>
          </section>

          <Separator className="bg-slate-700" />

          {/* Learning-rate schedule */}
          <section className="space-y-4">
            <SectionHeading>Learning-rate schedule</SectionHeading>
            <div>
              <Label htmlFor="scheduler_type" className="text-slate-300">
                Scheduler
              </Label>
              <Select
                value={config.scheduler_type || 'preset'}
                onValueChange={(value) =>
                  updateConfig(
                    'scheduler_type',
                    value === 'preset'
                      ? ''
                      : (value as
                          | 'cosine_decay_with_warmup'
                          | 'cosine_annealing_with_warmup'
                          | 'constant_with_warmup'),
                  )
                }
              >
                <SelectTrigger
                  id="scheduler_type"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-slate-800 border-slate-600 text-white">
                  <SelectItem value="preset">Use policy preset</SelectItem>
                  <SelectItem value="cosine_decay_with_warmup">
                    Cosine decay with warmup
                  </SelectItem>
                  <SelectItem value="cosine_annealing_with_warmup">
                    Cosine annealing with warmup
                  </SelectItem>
                  <SelectItem value="constant_with_warmup">Constant with warmup</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div>
                <Label htmlFor="scheduler_warmup_steps" className="text-slate-300">
                  Warmup steps
                </Label>
                <NumberInput
                  id="scheduler_warmup_steps"
                  value={config.scheduler_warmup_steps}
                  onChange={(v) => updateConfig('scheduler_warmup_steps', v)}
                  placeholder="preset"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
              <div>
                <Label htmlFor="scheduler_decay_steps" className="text-slate-300">
                  Decay horizon
                </Label>
                <NumberInput
                  id="scheduler_decay_steps"
                  value={config.scheduler_decay_steps}
                  onChange={(v) => updateConfig('scheduler_decay_steps', v)}
                  placeholder="preset"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
              <div>
                <Label htmlFor="scheduler_peak_lr" className="text-slate-300">
                  Peak LR
                </Label>
                <NumberInput
                  id="scheduler_peak_lr"
                  integer={false}
                  value={config.scheduler_peak_lr}
                  onChange={(v) => updateConfig('scheduler_peak_lr', v)}
                  placeholder="preset"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
              <div>
                <Label htmlFor="scheduler_floor_lr" className="text-slate-300">
                  Floor LR
                </Label>
                <NumberInput
                  id="scheduler_floor_lr"
                  integer={false}
                  value={config.scheduler_floor_lr}
                  onChange={(v) => updateConfig('scheduler_floor_lr', v)}
                  placeholder="preset"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
            </div>
            <p className="text-xs text-slate-500">
              SmolVLA Scene1 常用：peak 1e-4、warmup 500、cosine horizon 15000、floor 1e-5。
              留空则沿用 policy preset；显式值在 preset 之后生效。续训任务锁定 checkpoint 中的
              optimizer/scheduler，不会静默改写。
            </p>
          </section>

          <Separator className="bg-slate-700" />

          {/* Validation and monitoring */}
          <section className="space-y-4">
            <SectionHeading>Validation & monitoring</SectionHeading>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <Label htmlFor="eval_steps" className="text-slate-300">
                  Validation every N steps
                </Label>
                <NumberInput
                  id="eval_steps"
                  value={config.eval_steps}
                  onChange={(v) => updateConfig('eval_steps', v)}
                  placeholder="0 = disabled"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
              <div>
                <Label htmlFor="max_eval_samples" className="text-slate-300">
                  Max validation samples
                </Label>
                <NumberInput
                  id="max_eval_samples"
                  value={config.max_eval_samples}
                  onChange={(v) => updateConfig('max_eval_samples', v)}
                  placeholder="0 = all"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
              <div>
                <Label htmlFor="weight_norm_freq" className="text-slate-300">
                  Weight norm every N steps
                </Label>
                <NumberInput
                  id="weight_norm_freq"
                  value={config.weight_norm_freq}
                  onChange={(v) => updateConfig('weight_norm_freq', v)}
                  placeholder="250"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <Label htmlFor="dataset_eval_episodes" className="text-slate-300">
                  Validation episodes (explicit)
                </Label>
                <EpisodeListInput
                  value={config.dataset_eval_episodes}
                  onChange={(v) => updateConfig('dataset_eval_episodes', v)}
                />
                <p className="mt-1 text-xs text-slate-500">
                  Scene1：validation 0–11，train 由其余 episode 自动构成（12–63）。
                </p>
              </div>
              <div>
                <Label htmlFor="dataset_eval_split" className="text-slate-300">
                  Validation split (fraction, optional)
                </Label>
                <NumberInput
                  id="dataset_eval_split"
                  integer={false}
                  value={config.dataset_eval_split}
                  onChange={(v) => updateConfig('dataset_eval_split', v)}
                  placeholder="e.g. 0.2; leave empty for explicit episodes"
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
                <p className="mt-1 text-xs text-slate-500">
                  与显式 episodes 二选一；不要对 Scene1 使用比例重切。
                </p>
              </div>
            </div>
            {config.policy_type === 'smolvla' && (
              <div className="flex items-center space-x-3">
                <Switch
                  id="action_loss_groups"
                  checked={!!config.action_loss_groups}
                  onCheckedChange={(checked) =>
                    updateConfig(
                      'action_loss_groups',
                      checked
                        ? {
                            joints: [0, 1, 2, 3, 4, 6, 7, 8, 9, 10],
                            gripper_left: [5],
                            gripper_right: [11],
                          }
                        : null,
                    )
                  }
                />
                <Label htmlFor="action_loss_groups" className="text-slate-300">
                  Record grouped flow loss (10 relative joints + 2 absolute grippers, 12D)
                </Label>
              </div>
            )}
          </section>

          <Separator className="bg-slate-700" />

          {/* Logging & Checkpointing */}
          <section className="space-y-4">
            <SectionHeading>Logging & Checkpointing</SectionHeading>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <Label htmlFor="log_freq" className="text-slate-300">
                  Log Frequency
                </Label>
                <NumberInput
                  id="log_freq"
                  value={config.log_freq}
                  onChange={(v) => {
                    if (v !== undefined) updateConfig('log_freq', v);
                  }}
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
              <div>
                <Label htmlFor="save_freq" className="text-slate-300">
                  Save Frequency
                </Label>
                <NumberInput
                  id="save_freq"
                  value={config.save_freq}
                  onChange={(v) => {
                    if (v !== undefined) updateConfig('save_freq', v);
                  }}
                  className="bg-slate-900 border-slate-600 text-white rounded-lg"
                />
              </div>
            </div>
            <div className="flex items-center space-x-3">
              <Switch
                id="save_checkpoint"
                checked={config.save_checkpoint}
                onCheckedChange={(checked) => updateConfig('save_checkpoint', checked)}
              />
              <Label htmlFor="save_checkpoint" className="text-slate-300">
                Save Checkpoints
              </Label>
            </div>
            <div className="flex items-center space-x-3">
              <Switch
                id="resume"
                checked={config.resume}
                onCheckedChange={(checked) => updateConfig('resume', checked)}
              />
              <Label htmlFor="resume" className="text-slate-300">
                Resume from Checkpoint
              </Label>
            </div>
          </section>

          {config.wandb_enable && (
            <>
              <Separator className="bg-slate-700" />
              <section className="space-y-4">
                <SectionHeading>Weights & Biases</SectionHeading>
                <div>
                  <Label htmlFor="wandb_entity" className="text-slate-300">
                    W&B Entity (optional)
                  </Label>
                  <Input
                    id="wandb_entity"
                    value={config.wandb_entity || ''}
                    onChange={(e) =>
                      updateConfig('wandb_entity', e.target.value || undefined)
                    }
                    placeholder="your-username"
                    className="bg-slate-900 border-slate-600 text-white rounded-lg"
                  />
                </div>
                <div>
                  <Label htmlFor="wandb_notes" className="text-slate-300">
                    W&B Notes (optional)
                  </Label>
                  <Input
                    id="wandb_notes"
                    value={config.wandb_notes || ''}
                    onChange={(e) =>
                      updateConfig('wandb_notes', e.target.value || undefined)
                    }
                    placeholder="Training run notes..."
                    className="bg-slate-900 border-slate-600 text-white rounded-lg"
                  />
                </div>
                <div>
                  <Label htmlFor="wandb_mode" className="text-slate-300">
                    W&B Mode
                  </Label>
                  <Select
                    value={config.wandb_mode || 'online'}
                    onValueChange={(value) => updateConfig('wandb_mode', value)}
                  >
                    <SelectTrigger id="wandb_mode" className="bg-slate-900 border-slate-600 text-white rounded-lg">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="bg-slate-800 border-slate-600 text-white">
                      <SelectItem value="online">Online</SelectItem>
                      <SelectItem value="offline">Offline</SelectItem>
                      <SelectItem value="disabled">Disabled</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex items-center space-x-3">
                  <Switch
                    id="wandb_disable_artifact"
                    checked={config.wandb_disable_artifact}
                    onCheckedChange={(checked) =>
                      updateConfig('wandb_disable_artifact', checked)
                    }
                  />
                  <Label htmlFor="wandb_disable_artifact" className="text-slate-300">
                    Disable Artifacts
                  </Label>
                </div>
              </section>
            </>
          )}

          {!config.wandb_enable && <Separator className="bg-slate-700" />}

          {/* Misc */}
          <section className="space-y-4">
            <SectionHeading>Misc</SectionHeading>
            <div className="flex items-center space-x-3">
              <Switch
                id="use_policy_training_preset"
                checked={config.use_policy_training_preset}
                onCheckedChange={(checked) =>
                  updateConfig('use_policy_training_preset', checked)
                }
              />
              <Label htmlFor="use_policy_training_preset" className="text-slate-300">
                Use Policy Training Preset
              </Label>
            </div>
          </section>
        </CardContent>
      )}
    </Card>
  );
};

export default AdvancedCard;
