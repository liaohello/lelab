import React from "react";
import { AlertCircle, ExternalLink } from "lucide-react";
import { useHfAuth } from "@/contexts/HfAuthContext";
import HfTokenForm from "./HfTokenForm";

/**
 * Training-page banner. Rendered when there is no verified token: either
 * nothing is stored, or a stored token failed verification (proxy down /
 * rejected) — in that case the token is kept and the reason is shown.
 */
const HfAuthBanner: React.FC = () => {
  const { auth, refetch } = useHfAuth();

  if (auth.status === "authenticated" || auth.status === "loading") {
    return null;
  }

  const unverified = auth.status === "unverified";

  return (
    <div className="bg-amber-950/40 border border-amber-700/60 rounded-lg p-4 mb-6">
      <div className="flex items-start gap-3">
        <AlertCircle className="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" />
        <div className="flex-1 space-y-3">
          <div>
            <p className="text-sm text-amber-100 font-medium">
              {unverified
                ? "Saved Hugging Face token could not be verified"
                : "Hugging Face access required for cloud training"}
            </p>
            {unverified ? (
              <p className="text-xs text-amber-200/80 mt-1 break-words">
                {auth.error ?? "The Hub was not reachable."}{" "}
                <button
                  type="button"
                  onClick={() => void refetch()}
                  className="underline hover:text-amber-50"
                >
                  Re-check now
                </button>
              </p>
            ) : (
              <p className="text-xs text-amber-200/80 mt-1">
                Create a token at{" "}
                <a
                  href="https://huggingface.co/settings/tokens"
                  target="_blank"
                  rel="noreferrer"
                  className="underline hover:text-amber-50 inline-flex items-center gap-1"
                >
                  huggingface.co/settings/tokens
                  <ExternalLink className="w-3 h-3" />
                </a>{" "}
                with <span className="font-mono">Write</span> access, then paste it below
                (or paste a replacement token if the current one was revoked).
              </p>
            )}
          </div>
          <HfTokenForm />
        </div>
      </div>
    </div>
  );
};

export default HfAuthBanner;
