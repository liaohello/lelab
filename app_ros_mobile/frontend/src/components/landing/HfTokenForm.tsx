import React, { useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useHfAuth } from "@/contexts/HfAuthContext";

interface Props {
  /** Called after the token was validated and stored by the backend. */
  onSaved?: () => void;
  autoFocus?: boolean;
  compact?: boolean;
}

/**
 * Paste-a-token form shared by the top-bar dialog and the Training banner.
 * The token is validated by POST /hf-auth/login (whoami) and persisted to
 * <HF_HOME>/token on this machine, so it survives restarts.
 */
const HfTokenForm: React.FC<Props> = ({ onSaved, autoFocus = false, compact = false }) => {
  const { login } = useHfAuth();
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    const trimmed = token.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(trimmed);
      setToken("");
      onSaved?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-2">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          handleSave();
        }}
        className="flex gap-2"
      >
        <Input
          type="password"
          name="hf-token"
          placeholder="hf_..."
          value={token}
          autoFocus={autoFocus}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setToken(e.target.value)}
          disabled={submitting}
          aria-label="Hugging Face access token"
          className="bg-slate-900 border-slate-600 text-white placeholder:text-slate-500 font-mono"
        />
        <Button
          type="submit"
          disabled={submitting || !token.trim()}
          className={compact ? "bg-amber-600 hover:bg-amber-700 text-white" : "bg-amber-600 hover:bg-amber-700 text-white"}
        >
          {submitting ? (
            <>
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
              Verifying…
            </>
          ) : (
            "Save token"
          )}
        </Button>
      </form>
      {error ? <p className="text-xs text-red-300 break-words">{error}</p> : null}
    </div>
  );
};

export default HfTokenForm;
