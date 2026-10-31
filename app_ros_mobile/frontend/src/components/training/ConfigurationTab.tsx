import React from 'react';
import EssentialsCard from './config/EssentialsCard';
import GrootCard from './config/GrootCard';
import AdvancedCard from './config/AdvancedCard';
import TargetCard from './config/TargetCard';
import { ConfigComponentProps } from './types';
import { DatasetItem } from '@/lib/replayApi';
import { RunnerFlavor } from '@/lib/jobsApi';
import { CompositeReport } from '@/lib/compositesApi';

interface ConfigurationTabProps extends ConfigComponentProps {
  datasets: DatasetItem[];
  datasetsLoading: boolean;
  authenticated: boolean;
  flavors: RunnerFlavor[];
  hardwareLoading: boolean;
  onCompositeReport?: (report: CompositeReport | null) => void;
}

const ConfigurationTab: React.FC<ConfigurationTabProps> = ({
  config,
  updateConfig,
  datasets,
  datasetsLoading,
  authenticated,
  flavors,
  hardwareLoading,
  onCompositeReport,
}) => {
  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <TargetCard
        config={config}
        updateConfig={updateConfig}
        authenticated={authenticated}
        flavors={flavors}
        loading={hardwareLoading}
      />
      <EssentialsCard
        config={config}
        updateConfig={updateConfig}
        datasets={datasets}
        datasetsLoading={datasetsLoading}
        onCompositeReport={onCompositeReport}
      />
      <GrootCard config={config} updateConfig={updateConfig} />
      <AdvancedCard config={config} updateConfig={updateConfig} />
    </div>
  );
};

export default ConfigurationTab;
