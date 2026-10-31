import React, { useState } from "react";
import { AlertTriangle, Check, Copy, ExternalLink, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useHfAuth } from "@/contexts/HfAuthContext";
import { useToast } from "@/hooks/use-toast";
import HfTokenForm from "./HfTokenForm";

interface HfAuthDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Top-bar HF panel: paste-a-token login, verification state, and removal.
 * Opened from HfAuthChip on every page, so a token can be entered without
 * navigating to the Training page.
 */
const HfAuthDialog: React.FC<HfAuthDialogProps> = ({ open, onOpenChange }) => {
  const { auth, refetch, logout } = useHfAuth();
  const { toast } = useToast();
  const [copied, setCopied] = useState(false);
  const [refetching, setRefetching] = useState(false);
  const [removing, setRemoving] = useState(false);

  if (auth.status === "loading") return null;

  const loginCommand = auth.status === "unauthenticated" ? auth.loginCommand : "hf auth login";
  const unverified = auth.status === "unverified";

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(loginCommand);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (err) {
      console.warn("Clipboard write failed:", err);
    }
  };

  const handleRefetch = async () => {
    setRefetching(true);
    try {
      await refetch();
    } finally {
      setRefetching(false);
    }
  };

  const handleLogout = async () => {
    setRemoving(true);
    try {
      await logout();
      toast({ title: "Hugging Face token removed" });
    } catch (e) {
      toast({
        title: "Couldn't remove the token",
        description: e instanceof Error ? e.message : String(e),
        variant: "destructive",
      });
    } finally {
      setRemoving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-gray-900 border-gray-800 text-white sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle className={unverified ? "text-amber-200" : "text-white"}>
            {auth.status === "authenticated"
              ? "Hugging Face"
              : unverified
                ? "Hugging Face token needs attention"
                : "Connect Hugging Face"}
          </DialogTitle>
          <DialogDescription className="text-gray-400">
            {auth.status === "authenticated" ? (
              <>
                Signed in as <span className="text-emerald-300">{auth.username}</span>. The
                token is stored on this machine and reused after restarts.
              </>
            ) : unverified ? (
              <>
                A token is saved on this machine, but it could not be verified right
                now — cloud training and Hub downloads stay unavailable until the
                check succeeds.
              </>
            ) : (
              <>
                Paste a token from{" "}
                <a
                  href="https://huggingface.co/settings/tokens"
                  target="_blank"
                  rel="noreferrer"
                  className="underline hover:text-gray-200 inline-flex items-center gap-1"
                >
                  huggingface.co/settings/tokens
                  <ExternalLink className="w-3 h-3" />
                </a>{" "}
                (Write access, so trained policies can upload to your account).
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        {unverified ? (
          <div className="rounded-md border border-amber-700/60 bg-amber-950/40 p-3 text-xs text-amber-100 space-y-1">
            <div className="flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
              <p className="break-words">{auth.error ?? "Could not verify the saved token."}</p>
            </div>
            {auth.tokenPath ? (
              <p className="text-[11px] text-amber-200/70 font-mono break-all">
                saved at {auth.tokenPath}
              </p>
            ) : null}
          </div>
        ) : null}

        <div className="space-y-3">
          <HfTokenForm
            autoFocus={!unverified}
            onSaved={() => {
              toast({ title: "Hugging Face connected" });
              onOpenChange(false);
            }}
          />

          <details className="text-xs text-gray-500">
            <summary className="cursor-pointer hover:text-gray-300">
              Prefer the terminal? Use the HF CLI instead
            </summary>
            <div className="mt-2 flex items-center justify-between gap-2 rounded border border-gray-700 bg-gray-950 p-2">
              <code className="text-green-400">{loginCommand}</code>
              <button
                type="button"
                onClick={handleCopy}
                className="flex-shrink-0 text-gray-400 hover:text-gray-200"
                aria-label="Copy command"
              >
                {copied ? <Check className="w-4 h-4 text-green-400" /> : <Copy className="w-4 h-4" />}
              </button>
            </div>
          </details>
        </div>

        <div className="flex justify-end gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={handleRefetch}
            disabled={refetching}
            className="border-gray-600 bg-transparent text-gray-200 hover:bg-gray-800"
          >
            <RefreshCw className={`w-4 h-4 mr-2 ${refetching ? "animate-spin" : ""}`} />
            Recheck
          </Button>
          {auth.status === "authenticated" || unverified ? (
            <Button
              variant="outline"
              size="sm"
              onClick={handleLogout}
              disabled={removing}
              className="border-red-800 bg-transparent text-red-200 hover:bg-red-950/60"
            >
              {removing ? "Removing…" : "Remove token"}
            </Button>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
};

export default HfAuthDialog;
