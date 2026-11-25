package com.example.pepper_eventplaner

import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Base64
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.TextView
import com.aldebaran.qi.Future
import com.aldebaran.qi.sdk.QiContext
import com.aldebaran.qi.sdk.QiSDK
import com.aldebaran.qi.sdk.RobotLifecycleCallbacks
import com.aldebaran.qi.sdk.`object`.actuation.LookAtMovementPolicy
import com.aldebaran.qi.sdk.`object`.camera.TakePicture
import com.aldebaran.qi.sdk.`object`.conversation.BodyLanguageOption
import com.aldebaran.qi.sdk.`object`.geometry.Vector3
import com.aldebaran.qi.sdk.`object`.holder.AutonomousAbilitiesType
import com.aldebaran.qi.sdk.`object`.holder.Holder
import com.aldebaran.qi.sdk.`object`.touch.TouchSensor
import com.aldebaran.qi.sdk.builder.AnimateBuilder
import com.aldebaran.qi.sdk.builder.AnimationBuilder
import com.aldebaran.qi.sdk.builder.HolderBuilder
import com.aldebaran.qi.sdk.builder.LookAtBuilder
import com.aldebaran.qi.sdk.builder.SayBuilder
import com.aldebaran.qi.sdk.builder.TakePictureBuilder
import com.aldebaran.qi.sdk.builder.TransformBuilder
import com.aldebaran.qi.sdk.design.activity.RobotActivity
import fi.iki.elonen.NanoHTTPD
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Reine Gespraechs-App: Pepper hoert zu, schickt den Text + ein Gesichtsbild an das
 * Backend und spricht die Antwort. Das Gesichtsbild dient nur der lokalen
 * Personenerkennung fuer das ACT-Up-Gedaechtnis.
 */
class MainActivity : RobotActivity(), RobotLifecycleCallbacks {

    companion object {
        private const val TAG = "Pepper"
        private const val REQ_RECORD_AUDIO = 2001
        private const val TEXT_RECEIVER_PORT = 8080
        private const val FOCUS_TIMEOUT_MS = 120_000L

        // Neutrale Bezeichnungen fuer die Teilnehmenden -> Backend-Modus.
        // Die Zuordnung steht NUR hier (und im Versuchsplan), nicht auf dem Tablet.
        //   1 = Zustandsraum + Gedaechtnis   2 = nur Zustandsraum
        //   3 = nur Gedaechtnis              4 = nur Prompt
        private val BEDINGUNGEN = linkedMapOf("A" to 1, "B" to 2, "C" to 3, "D" to 4)
    }

    // ---- Studie: von der Versuchsleitung am Tablet gewaehlt ----
    @Volatile private var bedingung = "A"
    @Volatile private var modus = 1
    @Volatile private var teilnehmerId = ""

    private var qiContext: QiContext? = null
    private var takePictureFuture: Future<TakePicture>? = null
    private val cameraLock = Any()

    private var backendBaseUrl = "http://141.83.33.136:8000"
    @Volatile private var backendConfigured = false
    private val conversationId = java.util.UUID.randomUUID().toString()

    // ---- Spracherkennung ----
    private var speechRecognizer: SpeechRecognizer? = null
    private var recognizerIntent: Intent? = null
    private val mainHandler = Handler(Looper.getMainLooper())
    @Volatile private var freeSpeechEnabled = false
    @Volatile private var backendInFlight = false
    @Volatile private var isSpeaking = false
    @Volatile private var useExternalMic = false
    // Nur bei echten Uebergaengen "Ich hoere zu" sagen, nicht bei internen Neustarts
    @Volatile private var shouldAnnounceListening = false

    // ---- Fokus-Modus: Kopf beruehren -> Pepper schaut geradeaus ----
    private var headTouchSensor: TouchSensor? = null
    private var focusHolder: Holder? = null
    private var focusLookAtFuture: Future<Void>? = null
    @Volatile private var focusModeActive = false
    @Volatile private var lastHeadTouchMs = 0L
    private val focusLock = Any()
    private val focusHandler = Handler(Looper.getMainLooper())
    private val focusTimeoutRunnable = Runnable {
        Thread { stopFocusMode() }.start()
        setBubble("(ich schau mich wieder um)")
    }

    private var textReceiver: TextReceiverServer? = null

    // ================= Lifecycle =================

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        window.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN)

        QiSDK.register(this, this)
        ensureAudioPermission()
        initSpeechRecognizer()

        // Langes Druecken: Bedingung wechseln, ohne die App neu zu starten
        findViewById<Button>(R.id.btnBedingung)?.setOnLongClickListener {
            showStudienDialog(cancelable = true)
            true
        }
        showBackendAddressDialog()
    }

    override fun onDestroy() {
        QiSDK.unregister(this, this)
        stopTextReceiver()
        speechRecognizer?.destroy()
        speechRecognizer = null
        super.onDestroy()
    }

    override fun onRobotFocusGained(qiContext: QiContext) {
        this.qiContext = qiContext
        takePictureFuture = TakePictureBuilder.with(qiContext).buildAsync()
        setUpHeadTouch(qiContext)
        playGesture(R.raw.affirmation_a006)

        if (backendConfigured) startConversation()
        else setBubble("Bitte Backend-Adresse eingeben!")
    }

    override fun onRobotFocusLost() {
        qiContext = null
        takePictureFuture = null
        headTouchSensor?.removeAllOnStateChangedListeners()
        headTouchSensor = null
        focusHandler.removeCallbacks(focusTimeoutRunnable)
        focusLookAtFuture = null
        focusHolder = null
        focusModeActive = false
    }

    override fun onRobotFocusRefused(reason: String?) {
        Log.e(TAG, "Focus refused: $reason")
    }

    private fun showBackendAddressDialog() {
        val prefs = getSharedPreferences("pepper_eventplaner", MODE_PRIVATE)
        val startValue = prefs.getString("backend_base_url", backendBaseUrl) ?: backendBaseUrl

        val input = EditText(this).apply {
            setText(startValue)
            setSelection(text.length)
            inputType = android.text.InputType.TYPE_CLASS_TEXT
        }

        android.support.v7.app.AlertDialog.Builder(this)
            .setTitle("Backend Adresse")
            .setMessage("Adresse des Backends eingeben (z.B. http://141.83.33.136:8000)")
            .setView(input)
            .setCancelable(false)
            .setPositiveButton("Start") { _, _ ->
                val entered = input.text.toString().trim().trimEnd('/')
                if (entered.isNotBlank()) {
                    backendBaseUrl = entered
                    prefs.edit().putString("backend_base_url", entered).apply()
                }
                showStudienDialog(cancelable = false)
            }
            .show()
    }

    /**
     * Versuchsleitung waehlt Teilnehmer-ID und Bedingung (A-D).
     * Beim Start und spaeter per langem Druck auf den Bedingungs-Button - die App laeuft weiter.
     */
    private fun showStudienDialog(cancelable: Boolean) {
        val prefs = getSharedPreferences("pepper_eventplaner", MODE_PRIVATE)
        stopListening()   // waehrend der Auswahl nicht zuhoeren

        val idFeld = EditText(this).apply {
            hint = "Teilnehmer-ID, z.B. P07"
            setText(teilnehmerId.ifBlank { prefs.getString("teilnehmer_id", "") ?: "" })
            setSelection(text.length)
            inputType = android.text.InputType.TYPE_CLASS_TEXT
        }
        // Vorauswahl: beim Wechsel die naechste Bedingung, beim Start die zuletzt genutzte
        val buchstaben = BEDINGUNGEN.keys.toList()
        val vorauswahl = if (cancelable) {
            buchstaben[(buchstaben.indexOf(bedingung) + 1) % buchstaben.size]
        } else {
            prefs.getString("bedingung", "A") ?: "A"
        }
        val gruppe = RadioGroup(this)
        val knoepfe = buchstaben.associateWith { b ->
            RadioButton(this).apply {
                id = View.generateViewId()
                text = "Bedingung $b"
                textSize = 18f
            }.also { gruppe.addView(it) }
        }
        knoepfe[vorauswahl]?.isChecked = true

        val inhalt = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 16, 48, 0)
            addView(idFeld)
            addView(gruppe)
        }

        val dialog = android.support.v7.app.AlertDialog.Builder(this)
            .setTitle("Studie: Teilnehmer und Bedingung")
            .setView(inhalt)
            .setCancelable(false)
            .setPositiveButton("Starten") { _, _ ->
                val gewaehlt = knoepfe.entries.firstOrNull { it.value.isChecked }?.key ?: vorauswahl
                val id = idFeld.text.toString().trim().ifBlank { "ohne_id" }
                starteBedingung(gewaehlt, id)
                prefs.edit().putString("teilnehmer_id", id).putString("bedingung", gewaehlt).apply()
            }
        if (cancelable) {
            dialog.setNegativeButton("Abbrechen") { _, _ ->
                if (qiContext != null) startConversation()
            }
        }
        dialog.show()
    }

    private fun starteBedingung(neueBedingung: String, id: String) {
        bedingung = neueBedingung
        modus = BEDINGUNGEN[neueBedingung] ?: 1
        teilnehmerId = id
        Log.i(TAG, "Bedingung $bedingung (Modus $modus), Teilnehmer $teilnehmerId")
        runOnUiThread {
            findViewById<Button>(R.id.btnBedingung)?.text = "$bedingung · $teilnehmerId"
            findViewById<TextView>(R.id.txtUser)?.text = ""
            findViewById<TextView>(R.id.txtMemory)?.text = "Planung: –"
        }
        backendConfigured = true
        if (qiContext != null) startConversation()
    }

    private fun startConversation() {
        setBubble("(bereit)")
        if (!useExternalMic) startFreeSpeech()
    }

    // ================= UI =================

    private fun setBubble(text: String) = setText(R.id.txtBubble, "Pepper: $text")

    private fun setText(viewId: Int, text: String) {
        runOnUiThread {
            findViewById<TextView>(viewId)?.text = text
        }
    }

    private fun showMemory(json: JSONObject) {
        val themenArr = json.optJSONArray("erinnerte_themen")
        val themen = (0 until (themenArr?.length() ?: 0)).map { themenArr!!.getString(it) }
        val planung = "Planung: " + if (themen.isEmpty()) "–" else themen.joinToString(", ")

        // Ohne Gedaechtnis (Modus 2, 4): keine Person anzeigen - das Tablet soll nicht verraten,
        // dass Pepper in dieser Bedingung niemanden erkennt.
        if (!json.optBoolean("gedaechtnis", true)) {
            setText(R.id.txtMemory, planung)
            return
        }
        val personId = json.optString("person_id", "").takeIf { it.isNotBlank() && it != "null" }
        if (personId == null) {
            setText(R.id.txtMemory, "Gedächtnis: kein Gesicht erkannt")
            return
        }
        val name = json.optString("person_name", "").takeIf { it.isNotBlank() && it != "null" }
        val neu = json.optBoolean("person_neu", false)
        val wer = (name ?: "Unbekannt") + " ($personId)" + if (neu) " – neu" else " – wiedererkannt"
        setText(R.id.txtMemory, "Person: $wer\n$planung")
    }

    // ================= Sprechen =================

    private fun speak(text: String, onDone: (() -> Unit)? = null) {
        val context = qiContext
        if (context == null) {
            onDone?.let { cb -> runOnUiThread { cb() } }
            return
        }
        setBubble(text)
        Thread {
            isSpeaking = true
            try {
                SayBuilder.with(context)
                    .withText(text)
                    .withBodyLanguageOption(BodyLanguageOption.NEUTRAL)
                    .build()
                    .run()
            } catch (e: Exception) {
                Log.e(TAG, "Fehler beim Sprechen", e)
            } finally {
                isSpeaking = false
                onDone?.let { cb -> runOnUiThread { cb() } }
            }
        }.start()
    }

    /** Spricht und kehrt danach in die Hoer-Schleife zurueck. */
    private fun speakAndThenListen(text: String) {
        stopListening()
        speak(text) {
            if (!useExternalMic) {
                freeSpeechEnabled = true
                shouldAnnounceListening = true
                startListeningLoop(250L)
            }
        }
    }

    private fun playGesture(animationResId: Int) {
        val context = qiContext ?: return
        Thread {
            try {
                val animation = AnimationBuilder.with(context).withResources(animationResId).build()
                AnimateBuilder.with(context).withAnimation(animation).build().run()
            } catch (e: Exception) {
                Log.e(TAG, "Fehler bei der Geste", e)
            }
        }.start()
    }

    // ================= Spracherkennung =================

    private fun ensureAudioPermission() {
        if (android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.M) return
        val granted = checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) ==
                android.content.pm.PackageManager.PERMISSION_GRANTED
        if (!granted) requestPermissions(arrayOf(android.Manifest.permission.RECORD_AUDIO), REQ_RECORD_AUDIO)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_RECORD_AUDIO &&
            grantResults.firstOrNull() != android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            speak("Keine Berechtigung für das Mikrofon")
        }
    }

    private fun initSpeechRecognizer() {
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            Log.e("STT", "SpeechRecognizer ist nicht verfügbar!")
            return
        }
        speechRecognizer = SpeechRecognizer.createSpeechRecognizer(this)
        recognizerIntent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "de-DE")
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        }

        speechRecognizer?.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {
                setBubble("Ich höre zu!")
                if (shouldAnnounceListening) {
                    shouldAnnounceListening = false
                    speak("Ich höre zu.")
                }
            }

            override fun onError(error: Int) {
                Log.d("STT", "onError: $error")
                if (freeSpeechEnabled) startListeningLoop(400L)
            }

            override fun onResults(results: Bundle?) {
                resetFocusTimeout()
                val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                    ?.firstOrNull()?.trim().orEmpty()
                if (text.isBlank()) {
                    startListeningLoop(300L)
                    return
                }
                stopListening()
                handleUserText(text)
            }

            override fun onEndOfSpeech() {}
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(p0: Float) {}
            override fun onBufferReceived(p0: ByteArray?) {}
            override fun onPartialResults(p0: Bundle?) {}
            override fun onEvent(p0: Int, p1: Bundle?) {}
        })
    }

    private fun startFreeSpeech() {
        freeSpeechEnabled = true
        shouldAnnounceListening = true
        startListeningLoop()
    }

    private fun canListenNow(): Boolean =
        freeSpeechEnabled && !backendInFlight && !useExternalMic && !isSpeaking

    private fun startListeningLoop(delayMs: Long = 200L) {
        if (!canListenNow()) return
        if (speechRecognizer == null || recognizerIntent == null) initSpeechRecognizer()

        mainHandler.postDelayed({
            if (!canListenNow()) return@postDelayed
            try {
                speechRecognizer?.startListening(recognizerIntent)
            } catch (e: Exception) {
                Log.e("STT", "startListening failed", e)
                startListeningLoop(500L)
            }
        }, delayMs)
    }

    private fun stopListening() {
        freeSpeechEnabled = false
        mainHandler.removeCallbacksAndMessages(null)
        try {
            speechRecognizer?.stopListening()
            speechRecognizer?.cancel()
        } catch (e: Exception) {
            Log.e("STT", "stopListening failed", e)
        }
    }

    // ================= Backend =================

    /** Gemeinsamer Einstieg fuer internes und externes Mikrofon. */
    private fun handleUserText(text: String) {
        if (backendInFlight) {
            Log.w(TAG, "Eingabe ignoriert, Backend ist noch beschäftigt: $text")
            return
        }
        backendInFlight = true
        setText(R.id.txtUser, "Du: $text")
        speak("Alles klar.")
        askBackend(text)
    }

    private fun isBackendHealthy(): Boolean = try {
        val conn = (URL("$backendBaseUrl/health").openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = 3000
            readTimeout = 3000
        }
        val ok = conn.responseCode in 200..299
        conn.disconnect()
        ok
    } catch (e: Exception) {
        false
    }

    private fun waitForBackend(maxAttempts: Int = 4): Boolean {
        var delayMs = 1500L
        repeat(maxAttempts) { attempt ->
            if (isBackendHealthy()) return true
            setBubble("Ich verbinde mich gerade mit dem Backend...")
            if (attempt < maxAttempts - 1) {
                Thread.sleep(delayMs)
                delayMs = (delayMs * 2).coerceAtMost(8000L)
            }
        }
        return false
    }

    /** Frisches Kamerabild (blockierend) - nur fuer die Gesichtserkennung im Backend. */
    private fun captureFaceImage(): ByteArray? = try {
        synchronized(cameraLock) {
            val tp = takePictureFuture ?: return null
            val buffer = tp.get().async().run().get().image.value.data
            buffer.rewind()
            ByteArray(buffer.remaining()).also { buffer.get(it) }
        }
    } catch (e: Exception) {
        Log.e(TAG, "Gesichtsbild konnte nicht aufgenommen werden", e)
        null
    }

    private fun askBackend(userText: String) {
        Thread {
            val reply: String = try {
                if (!waitForBackend()) {
                    "Mein Backend startet noch oder ist gerade nicht erreichbar."
                } else {
                    val json = postChat(userText, captureFaceImage())
                    if (json.optString("mode") != "error") showMemory(json)
                    json.optString("text", "").ifBlank { "Dazu fällt mir gerade nichts ein." }
                }
            } catch (e: Exception) {
                Log.e("BACKEND", "Chat-Anfrage fehlgeschlagen", e)
                "Ich konnte mein Backend gerade nicht erreichen, überprüfe den Backend-Status bitte."
            }

            // "Alles klar." soll erst ausgesprochen sein, bevor die Antwort kommt
            while (isSpeaking) Thread.sleep(100)
            backendInFlight = false
            runOnUiThread { speakAndThenListen(reply) }
        }.start()
    }

    private fun postChat(userText: String, faceBytes: ByteArray?): JSONObject {
        // Eigener Endpunkt pro Modus: /chat/1 ... /chat/4
        val conn = (URL("$backendBaseUrl/chat/$modus").openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            setRequestProperty("Content-Type", "application/json")
            doOutput = true
            connectTimeout = 10000
            readTimeout = 30000
        }
        val body = JSONObject().apply {
            put("conversation_id", conversationId)
            put("teilnehmer_id", teilnehmerId)
            put("text", userText)
            if (faceBytes != null && faceBytes.isNotEmpty()) {
                put("face_image_base64", Base64.encodeToString(faceBytes, Base64.NO_WRAP))
            }
        }.toString()

        conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
        val code = conn.responseCode
        val response = (if (code in 200..299) conn.inputStream else conn.errorStream)
            ?.bufferedReader()?.use { it.readText() }.orEmpty()
        conn.disconnect()

        if (code !in 200..299) throw IllegalStateException("Backend-Fehler $code: $response")
        return JSONObject(response)
    }

    // ================= Externes Mikrofon (Handy schickt Text per HTTP) =================

    fun onToggleMicMode(view: View) {
        useExternalMic = !useExternalMic
        if (useExternalMic) {
            stopListening()
            shouldAnnounceListening = false
            startTextReceiver()
            speak("Ich höre jetzt über das externe Mikrofon.")
        } else {
            stopTextReceiver()
            speak("Ich höre jetzt wieder über mein eigenes Mikrofon.") { startFreeSpeech() }
        }
        runOnUiThread {
            findViewById<Button>(R.id.btnMicMode)?.text =
                if (useExternalMic) "Mikrofon: EXTERN (Handy)" else "Mikrofon: INTERN (Pepper)"
        }
    }

    private inner class TextReceiverServer(port: Int) : NanoHTTPD(port) {
        override fun serve(session: IHTTPSession): Response {
            if (session.method != Method.POST) {
                return newFixedLengthResponse(Response.Status.METHOD_NOT_ALLOWED, "text/plain", "Nur POST erlaubt")
            }
            return try {
                val body = HashMap<String, String>()
                session.parseBody(body)
                val text = JSONObject(body["postData"] ?: "").optString("text", "").trim()
                if (text.isBlank()) {
                    return newFixedLengthResponse(Response.Status.BAD_REQUEST, "text/plain", "Kein Text im Body")
                }
                runOnUiThread {
                    if (useExternalMic) {
                        resetFocusTimeout()
                        handleUserText(text)
                    }
                }
                newFixedLengthResponse(Response.Status.OK, "application/json", "{\"ok\":true}")
            } catch (e: Exception) {
                Log.e(TAG, "TextReceiver serve failed", e)
                newFixedLengthResponse(Response.Status.INTERNAL_ERROR, "text/plain", "Fehler: ${e.message}")
            }
        }
    }

    private fun startTextReceiver() {
        if (textReceiver != null) return
        try {
            textReceiver = TextReceiverServer(TEXT_RECEIVER_PORT).also {
                it.start(NanoHTTPD.SOCKET_READ_TIMEOUT, false)
            }
            Log.i(TAG, "TextReceiver läuft auf Port $TEXT_RECEIVER_PORT")
        } catch (e: Exception) {
            Log.e(TAG, "TextReceiver konnte nicht starten!", e)
        }
    }

    private fun stopTextReceiver() {
        try {
            textReceiver?.stop()
        } catch (e: Exception) {
            Log.e(TAG, "TextReceiver stop failed", e)
        }
        textReceiver = null
    }

    // ================= Fokus-Modus (Kopfsensor) =================

    private fun setUpHeadTouch(context: QiContext) {
        try {
            val sensor = context.touch.getSensor("Head/Touch")
            sensor.addOnStateChangedListener { state ->
                if (!state.touched) return@addOnStateChangedListener
                val now = System.currentTimeMillis()
                if (now - lastHeadTouchMs < 1000) return@addOnStateChangedListener
                lastHeadTouchMs = now
                startFocusMode()
            }
            headTouchSensor = sensor
        } catch (e: Exception) {
            Log.e(TAG, "Kopf-Touch-Sensor konnte nicht registriert werden!", e)
        }
    }

    // Nicht auf dem UI-Thread aufrufen (build/get blockieren)
    private fun startFocusMode() {
        val context = qiContext ?: return
        synchronized(focusLock) {
            try {
                if (focusHolder == null) {
                    focusHolder = HolderBuilder.with(context)
                        .withAutonomousAbilities(AutonomousAbilitiesType.BASIC_AWARENESS)
                        .build()
                    focusHolder?.async()?.hold()?.get()
                }
                focusLookAtFuture?.requestCancellation()
                val target = context.actuation.robotFrame()
                    .makeAttachedFrame(TransformBuilder.create().fromTranslation(Vector3(1.0, 0.0, 1.5)))
                    .frame()
                val lookAt = LookAtBuilder.with(context).withFrame(target).build()
                lookAt.policy = LookAtMovementPolicy.HEAD_ONLY
                focusLookAtFuture = lookAt.async().run()
                focusModeActive = true
            } catch (e: Exception) {
                Log.e(TAG, "Fokus-Modus konnte nicht gestartet werden", e)
            }
        }
        setBubble("Ich schaue jetzt wieder geradeaus!")
        resetFocusTimeout()
    }

    private fun stopFocusMode() {
        synchronized(focusLock) {
            focusLookAtFuture?.requestCancellation()
            focusLookAtFuture = null
            try {
                focusHolder?.async()?.release()?.get()
            } catch (e: Exception) {
                Log.e(TAG, "Fokus-Holder konnte nicht freigegeben werden", e)
            }
            focusHolder = null
            focusModeActive = false
        }
        focusHandler.removeCallbacks(focusTimeoutRunnable)
    }

    private fun resetFocusTimeout() {
        focusHandler.removeCallbacks(focusTimeoutRunnable)
        if (focusModeActive) focusHandler.postDelayed(focusTimeoutRunnable, FOCUS_TIMEOUT_MS)
    }
}
