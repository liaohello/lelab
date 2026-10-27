import React from "react";
import { Activity } from "lucide-react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import HfAuthChip from "./HfAuthChip";

const LandingTopBar: React.FC = () => {
  return (
    <header className="sticky top-0 z-30 w-full border-b border-gray-800 bg-black/95 backdrop-blur supports-[backdrop-filter]:bg-black/70">
      <div className="mx-auto flex h-12 max-w-7xl items-center justify-between px-4">
        <div className="flex items-center gap-2">
          <img
            src="/lovable-uploads/5e648747-34b7-4d8f-93fd-4dbd00aeeefc.png"
            alt="LeLab"
            className="h-7 w-7"
          />
          <span className="text-base font-semibold tracking-tight text-white">
            LeLab
          </span>
        </div>
        <div className="flex items-center gap-2">
          <Button
            asChild
            size="sm"
            variant="ghost"
            className="text-gray-300 hover:bg-gray-800 hover:text-white"
          >
            <Link to="/system">
              <Activity className="h-4 w-4" />
              <span className="hidden sm:inline">系统</span>
            </Link>
          </Button>
          <Button
            asChild
            size="sm"
            variant="ghost"
            className="text-gray-300 hover:bg-gray-800 hover:text-white"
          >
            <Link to="/storage">
              <Activity className="h-4 w-4" />
              <span className="hidden sm:inline">存储</span>
            </Link>
          </Button>
          <Button
            asChild
            size="sm"
            variant="ghost"
            className="text-gray-300 hover:bg-gray-800 hover:text-white"
          >
            <Link to="/dataset-viewer">
              <Activity className="h-4 w-4" />
              <span className="hidden sm:inline">数据</span>
            </Link>
          </Button>
          <Button
            asChild
            size="sm"
            variant="ghost"
            className="text-gray-300 hover:bg-gray-800 hover:text-white"
          >
            <Link to="/models">
              <Activity className="h-4 w-4" />
              <span className="hidden sm:inline">权重</span>
            </Link>
          </Button>
          <Button
            asChild
            size="sm"
            variant="ghost"
            className="text-gray-300 hover:bg-gray-800 hover:text-white"
          >
            <Link to="/system-storage">
              <Activity className="h-4 w-4" />
              <span className="hidden sm:inline">全部</span>
            </Link>
          </Button>
          <HfAuthChip />
        </div>
      </div>
    </header>
  );
};

export default LandingTopBar;
