package xyz.fenever.assistant.core;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.nio.charset.StandardCharsets;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.Signature;
import java.security.spec.ECGenParameterSpec;

import org.junit.Test;

/** M-7：更新清单签名的客户端半边。用临时 P-256 密钥跑完整 decide 路径。 */
public class ReleasePlanSignatureTest {

    private static final String DIGEST =
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    private static final String APP = "https://ai.fenever.xyz/app/";
    private static final String URL = "https://ai.fenever.xyz/site/android.apk";

    private static KeyPair keyPair() throws Exception {
        KeyPairGenerator gen = KeyPairGenerator.getInstance("EC");
        gen.initialize(new ECGenParameterSpec("secp256r1"));
        return gen.generateKeyPair();
    }

    private static String hex(byte[] bytes) {
        StringBuilder out = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) out.append(String.format("%02x", b & 0xff));
        return out.toString();
    }

    private static String publicHex(KeyPair pair) {
        return hex(pair.getPublic().getEncoded());
    }

    private static String sign(KeyPair pair, String payload) throws Exception {
        Signature signer = Signature.getInstance("SHA256withECDSA");
        signer.initSign(pair.getPrivate());
        signer.update(payload.getBytes(StandardCharsets.UTF_8));
        return hex(signer.sign());
    }

    private static String payload(String digest) {
        return "0.15\nfenver-0.15.apk\n" + digest + "\n12345";
    }

    private static String release(String digest, String signature) {
        String sigField = signature == null ? ""
                : ",\"apk_signature\":\"" + signature + "\"";
        return "{\"tag_name\":\"v0.15\",\"assets\":[{\"name\":\"fenver-0.15.apk\","
                + "\"size\":12345,\"browser_download_url\":\"" + URL + "\"}],"
                + "\"apk_sha256\":\"" + digest + "\"" + sigField + "}";
    }

    @Test
    public void aCorrectlySignedReleaseIsAvailable() throws Exception {
        KeyPair pair = keyPair();
        String sig = sign(pair, payload(DIGEST));
        ReleasePlan.Decision d = ReleasePlan.decide(
                "0.14", release(DIGEST, sig), APP, true, publicHex(pair));
        assertEquals(ReleasePlan.Kind.AVAILABLE, d.kind);
        assertEquals(DIGEST, d.sha256);
    }

    @Test
    public void aTamperedDigestBreaksTheSignature() throws Exception {
        KeyPair pair = keyPair();
        String sig = sign(pair, payload(DIGEST));
        String other = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";
        ReleasePlan.Decision d = ReleasePlan.decide(
                "0.14", release(other, sig), APP, true, publicHex(pair));
        assertEquals(ReleasePlan.Kind.UNUSABLE, d.kind);
        assertTrue(d.reason.contains("签名"));
    }

    @Test
    public void aMissingSignatureIsUnusableWhenRequired() {
        ReleasePlan.Decision d = ReleasePlan.decide(
                "0.14", release(DIGEST, null), APP, true, ReleasePlan.UPDATE_PUBLIC_KEY_HEX);
        assertEquals(ReleasePlan.Kind.UNUSABLE, d.kind);
        assertTrue(d.reason.contains("签名"));
    }

    @Test
    public void theSameResponseStillWorksForOldCallersWithoutTheFlag() {
        ReleasePlan.Decision d = ReleasePlan.decide("0.14", release(DIGEST, null), APP, false,
                ReleasePlan.UPDATE_PUBLIC_KEY_HEX);
        assertEquals(ReleasePlan.Kind.AVAILABLE, d.kind);
    }

    @Test
    public void theBundledPublicKeyIsWellFormed() {
        assertTrue(ReleasePlan.UPDATE_PUBLIC_KEY_HEX.length() > 100);
        assertTrue(ReleasePlan.UPDATE_PUBLIC_KEY_HEX.startsWith("30593013"));
    }
}
