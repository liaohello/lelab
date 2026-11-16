export interface TrainingConfig {
  /** `ssh_remote` runs on the configured training server (see /system). */
  target: {
    runner: "local" | "hf_cloud" | "ssh_remote";
    flavor?: string;
    /** GPU index on the training server; undefined = auto (freest card). */
    gpu?: number;
    /** Refuse to start below this much free VRAM (MB). */
    min_free_vram_mb?: number;
  };

  // Dataset configuration
  dataset_repo_id: string;
  /** Ordered member datasets. More than one ⇒ a composite on the training server. */
  dataset_repo_ids: string[];
  /** Sampling weights across members: frame-proportional / equal / custom. */
  dataset_weights_mode: "frames" | "equal" | "custom";
  dataset_weights?: number[];

  // Policy configuration
  policy_type: string;
  /** Optional remote/local path or Hugging Face id for fine-tuning. */
  policy_pretrained_path?: string;

  // Core training parameters
  steps: number;
  batch_size: number;
  seed?: number;
  num_workers: number;

  // Logging and checkpointing
  log_freq: number;
  save_freq: number;
  save_checkpoint: boolean;

  // Output configuration
  resume: boolean;

  // Weights & Biases
  wandb_enable: boolean;
  wandb_project?: string;
  wandb_entity?: string;
  wandb_notes?: string;
  wandb_mode?: string;
  wandb_disable_artifact: boolean;

  // Policy-specific parameters
  policy_device?: string;
  policy_use_amp: boolean;

  // Optimizer parameters
  optimizer_type?: string;
  optimizer_lr?: number;
  optimizer_weight_decay?: number;
  optimizer_grad_clip_norm?: number;

  // Scheduler overrides (applied after the policy preset; empty ⇒ preset).
  scheduler_type?: "cosine_decay_with_warmup" | "cosine_annealing_with_warmup" | "constant_with_warmup" | "";
  scheduler_warmup_steps?: number;
  scheduler_decay_steps?: number;
  scheduler_peak_lr?: number;
  scheduler_floor_lr?: number;

  // Validation loss + monitoring cadence.
  eval_steps?: number;
  max_eval_samples?: number;
  /** Explicit held-out episodes (comma-separated in the form). */
  dataset_eval_episodes?: number[];
  /** Ratio-based alternative; never combined with eval episodes. */
  dataset_eval_split?: number;
  weight_norm_freq?: number;
  /** Optional SmolVLA 12D grouped flow-loss diagnostics. */
  action_loss_groups?: Record<string, number[]> | null;

  // Advanced configuration
  use_policy_training_preset: boolean;

  // GR00T-specific configuration (only used when policy_type === "groot").
  dataset_image_transforms_enable?: boolean;
  policy_base_model_path?: string;
  policy_embodiment_tag?: string;
  policy_chunk_size?: number;
  policy_n_action_steps?: number;
  policy_use_relative_actions?: boolean;
  policy_relative_exclude_joints?: string[];
  policy_use_bf16?: boolean;
  // FastWAM-specific (only used when policy_type === "fastwam"). Its config
  // defaults to 7 actions / 8 state dims, which silently describes a different
  // robot body, so the backend refuses to build the command without both.
  policy_action_dim?: number;
  policy_proprio_dim?: number;
}

export type TrainingRunState =
  | "running"
  | "pause_requested"
  | "paused"
  | "pause_failed"
  | "resuming"
  | "done"
  | "failed"
  | "interrupted";

export interface TrainingStatus {
  training_active: boolean;
  state: TrainingRunState;
  current_step: number;
  total_steps: number;
  current_loss?: number;
  current_lr?: number;
  grad_norm?: number;
  epoch_time?: number;
  eta_seconds?: number;
  available_controls: {
    stop_training: boolean;
    pause_training: boolean;
    resume_training: boolean;
  };
}

export interface LogEntry {
  timestamp: number;
  message: string;
}

export interface ConfigComponentProps {
  config: TrainingConfig;
  updateConfig: <T extends keyof TrainingConfig>(
    key: T,
    value: TrainingConfig[T]
  ) => void;
}
